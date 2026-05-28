"""
paper2pod: arXiv ID -> LLM-narrated MP3 -> private podcast feed.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    export FEED_BASE_URL=https://<user>.github.io/<repo>/audio
    python paper2pod.py 2605.28405

Outputs:
    scripts/<id>.txt   plain-text script the LLM wrote
    audio/<id>.mp3     synthesized narration
    feed.xml           appended-to RSS feed (subscribe to this in Pocket Casts etc.)
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from openai import OpenAI
from bs4 import BeautifulSoup

# --- config ----------------------------------------------------------------

SCRIPTS_DIR = Path("scripts")
AUDIO_DIR = Path("audio")
FEED_PATH = Path("feed.xml")
PUBLIC_BASE_URL = os.environ.get(
    "FEED_BASE_URL", "https://example.github.io/paper-pod/audio"
).rstrip("/")

LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "alloy")
SIM_TTS_BASE_URL = os.environ.get(
    "SIM_TTS_BASE_URL", "http://10.183.39.205:5001"
).rstrip("/")

# --- fetchers --------------------------------------------------------------


def fetch_metadata(arxiv_id: str) -> dict:
    """Title, abstract, authors via the public arXiv Atom API."""
    r = requests.get(
        f"http://export.arxiv.org/api/query?id_list={arxiv_id}", timeout=30
    )
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None:
        raise RuntimeError(f"No arXiv entry for {arxiv_id}")
    return {
        "title": " ".join(entry.find("a:title", ns).text.split()),
        "abstract": entry.find("a:summary", ns).text.strip(),
        "authors": [a.find("a:name", ns).text for a in entry.findall("a:author", ns)],
    }


def fetch_paper_text(arxiv_id: str) -> str:
    """
    Try arXiv's native HTML, then ar5iv fallback, then PDF text extraction.
    HTML beats PDF: no two-column reflow, no broken hyphenation, sections are clean.
    """
    for url in (
        f"https://arxiv.org/html/{arxiv_id}",
        f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
    ):
        try:
            r = requests.get(url, timeout=45, headers={"User-Agent": "paper2pod/0.1"})
            if r.status_code == 200 and len(r.text) > 2000:
                print(f"      using HTML: {url}")
                return html_to_text(r.text)
        except requests.RequestException:
            continue

    # PDF fallback
    print("      HTML unavailable, falling back to PDF extraction")
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    r = requests.get(pdf_url, timeout=60)
    r.raise_for_status()
    pdf_path = Path(f"/tmp/{arxiv_id}.pdf")
    pdf_path.write_bytes(r.content)
    try:
        import pypdf

        reader = pypdf.PdfReader(str(pdf_path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    finally:
        pdf_path.unlink(missing_ok=True)


def html_to_text(html: str) -> str:
    """Strip nav/headers/footnotes/references, return readable body text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "figure"]):
        tag.decompose()
    # Drop the references section — pure noise for TTS
    for h in soup.find_all(re.compile("^h[1-3]$")):
        if h.get_text(strip=True).lower() in {"references", "bibliography"}:
            for sibling in list(h.find_next_siblings()):
                sibling.decompose()
            h.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# --- script generation -----------------------------------------------------

SCRIPT_PROMPT = """You are turning a research paper into a short spoken summary with highlights, for a knowledgeable ML practitioner listening on a commute. Target ~5–6 minutes, roughly 900–1300 words. Tight, not exhaustive.

Paper title: {title}
Authors: {authors}

Full paper text (truncated if long):
---
{paper_text}
---

Write a monologue script. Structure:
1. One-sentence hook: the title and why this paper matters.
2. The core contribution in two or three sentences — what problem it solves and the key idea.
3. The highlights: the handful of things actually worth knowing. Lead with concrete results and numbers where they exist ("cuts inference latency by forty percent", "beats the prior best by three points on the benchmark"). Call out anything surprising or counterintuitive.
4. One sentence on the most important limitation or caveat — only if there's a notable one.
5. A one-sentence takeaway: who should care and what to do with this.

Hard rules for TTS:
- Translate every equation into prose. Never read LaTeX or symbols.
- Spell out acronyms on first mention, then use them freely.
- Strip every inline citation: "[12]", "(Smith et al., 2023)", "(see Section 4)".
- Spoken phrasing — short sentences, natural transitions. No headings, no bullet points, no markdown, no stage directions. Continuous prose.
- Do NOT walk through every section. Summarize and highlight; skip methodology detail unless it's the actual contribution.

Output only the script text, no preamble, no closing remarks."""


def generate_script(paper_text: str, meta: dict, client: OpenAI) -> str:
    # Most papers fit; truncate gracefully if not.
    paper_text = paper_text[:120_000]
    prompt = SCRIPT_PROMPT.format(
        title=meta["title"],
        authors=", ".join(meta["authors"]),
        paper_text=paper_text,
    )
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


# --- TTS -------------------------------------------------------------------


def synthesize(script: str, wav_path: Path, voice: str = DEFAULT_VOICE) -> None:
    """
    Synthesize with SimTTS (/tts_v2 endpoint).
    Splits on paragraph boundaries and concatenates the returned WAV audio.
    """
    chunks = []
    paragraphs = [p.strip() for p in script.split("\n\n") if p.strip()]
    for i, para in enumerate(paragraphs):
        resp = requests.post(
            f"{SIM_TTS_BASE_URL}/tts_v2",
            json={"prompt": para, "max_tokens": 1200, "include_header": True},
            timeout=120,
        )
        resp.raise_for_status()
        chunks.append(resp.content)
        print(f"      synthesized paragraph {i + 1}/{len(paragraphs)}", end="\r")

    print()
    # Concatenate raw audio — write first chunk as-is (has WAV header),
    # then append raw PCM data from subsequent chunks (skip 44-byte WAV header).
    with open(wav_path, "wb") as f:
        for i, chunk in enumerate(chunks):
            if i == 0:
                f.write(chunk)
            else:
                f.write(chunk[44:])  # skip WAV header


def wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "96k") -> int:
    """Re-encode WAV to MP3 (smaller, supported by every podcast app). Returns size in bytes."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
         "-b:a", bitrate, "-ar", "24000", str(mp3_path)],
        check=True,
    )
    return mp3_path.stat().st_size


# --- RSS feed --------------------------------------------------------------

FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Paper Pod</title>
    <description>Auto-narrated arXiv papers</description>
    <link>{base}</link>
    <language>en-us</language>
    <itunes:author>paper2pod</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
  </channel>
</rss>
"""


def update_feed(feed_path: Path, episode: dict) -> None:
    if not feed_path.exists():
        feed_path.write_text(FEED_TEMPLATE.format(base=PUBLIC_BASE_URL))

    tree = ET.parse(feed_path)
    channel = tree.getroot().find("channel")

    # Replace if this guid already exists (re-runs are idempotent)
    for existing in channel.findall("item"):
        if existing.findtext("guid") == episode["guid"]:
            channel.remove(existing)
            break

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = episode["title"]
    ET.SubElement(item, "description").text = episode["description"]
    ET.SubElement(item, "guid").text = episode["guid"]
    ET.SubElement(item, "pubDate").text = episode["pubDate"]
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", episode["audio_url"])
    enc.set("type", "audio/mpeg")
    enc.set("length", str(episode["length"]))

    tree.write(feed_path, xml_declaration=True, encoding="utf-8")


# --- main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="arXiv ID -> narrated MP3 -> RSS")
    ap.add_argument("arxiv_id", help="e.g. 2605.28405")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--skip-feed", action="store_true", help="don't touch feed.xml")
    ap.add_argument("--keep-wav", action="store_true")
    args = ap.parse_args()

    SCRIPTS_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)

    aid = args.arxiv_id.strip()
    print(f"[1/5] Fetching metadata for {aid} ...")
    meta = fetch_metadata(aid)
    print(f"      {meta['title']}")

    print(f"[2/5] Fetching paper body ...")
    paper_text = fetch_paper_text(aid)
    print(f"      {len(paper_text):,} chars")

    print(f"[3/5] Generating script with {LLM_MODEL} ...")
    client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
    script = generate_script(paper_text, meta, client)
    script_path = SCRIPTS_DIR / f"{aid}.txt"
    script_path.write_text(script)
    print(f"      {len(script.split()):,} words -> {script_path}")

    print(f"[4/5] Synthesizing audio with Kokoro (voice={args.voice}) ...")
    wav_path = AUDIO_DIR / f"{aid}.wav"
    mp3_path = AUDIO_DIR / f"{aid}.mp3"
    synthesize(script, wav_path, voice=args.voice)
    size = wav_to_mp3(wav_path, mp3_path)
    if not args.keep_wav:
        wav_path.unlink()
    print(f"      {mp3_path} ({size / 1024 / 1024:.1f} MB)")

    if not args.skip_feed:
        print(f"[5/5] Updating RSS feed ...")
        episode = {
            "title": meta["title"],
            "description": meta["abstract"][:600],
            "guid": aid,
            "pubDate": datetime.now(timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            ),
            "audio_url": f"{PUBLIC_BASE_URL}/{aid}.mp3",
            "length": size,
        }
        update_feed(FEED_PATH, episode)
        print(f"      appended to {FEED_PATH}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
