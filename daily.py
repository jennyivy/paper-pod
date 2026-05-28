"""
daily.py: fetch the N newest papers in an arXiv category and run each
through the paper2pod pipeline. Dedupes against seen.json so running it
twice a day (or day-to-day overlap) never reprocesses a paper.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    export FEED_BASE_URL=https://<user>.github.io/paper-pod/audio
    python daily.py --category cs.AI --count 10

Intended for cron. See README.
"""

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from openai import OpenAI

import paper2pod as p2p

SEEN_PATH = Path("seen.json")


def recent_ids(category: str, n: int) -> list[str]:
    """
    Newest-first arXiv IDs for a category, via the public API.
    Mirrors what /list/<category>/recent shows. Versions stripped (2605.28405v2 -> 2605.28405).

    To instead match the daily *announcement* feed exactly, swap this for the RSS
    endpoint https://rss.arxiv.org/rss/<category> — see README.
    """
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query=cat:{category}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={n}"
    )
    r = requests.get(url, timeout=30, headers={"User-Agent": "paper2pod/0.1"})
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    ids = []
    for entry in ET.fromstring(r.text).findall("a:entry", ns):
        raw = entry.find("a:id", ns).text  # e.g. http://arxiv.org/abs/2605.28405v1
        aid = raw.rsplit("/abs/", 1)[-1].split("v")[0]
        ids.append(aid)
    return ids


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0))


def process_one(aid: str, client: OpenAI, voice: str) -> None:
    """Run a single paper through fetch -> script -> TTS -> feed (reuses paper2pod)."""
    meta = p2p.fetch_metadata(aid)
    print(f"      {meta['title']}")
    text = p2p.fetch_paper_text(aid)
    script = p2p.generate_script(text, meta, client)
    (p2p.SCRIPTS_DIR / f"{aid}.txt").write_text(script)

    wav = p2p.AUDIO_DIR / f"{aid}.wav"
    mp3 = p2p.AUDIO_DIR / f"{aid}.mp3"
    p2p.synthesize(script, wav, voice=voice)
    size = p2p.wav_to_mp3(wav, mp3)
    wav.unlink(missing_ok=True)

    p2p.update_feed(
        p2p.FEED_PATH,
        {
            "title": meta["title"],
            "description": meta["abstract"][:600],
            "guid": aid,
            "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "audio_url": f"{p2p.PUBLIC_BASE_URL}/{aid}.mp3",
            "length": size,
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="cs.AI", help="cs.AI, cs.CL, cs.SD, ...")
    ap.add_argument("--count", type=int, default=10, help="papers to add per run")
    ap.add_argument("--voice", default=p2p.DEFAULT_VOICE)
    args = ap.parse_args()

    p2p.SCRIPTS_DIR.mkdir(exist_ok=True)
    p2p.AUDIO_DIR.mkdir(exist_ok=True)

    seen = load_seen()
    # Over-fetch so that skipping already-seen papers still leaves enough new ones.
    candidates = recent_ids(args.category, args.count * 3)
    todo = [a for a in candidates if a not in seen][: args.count]

    if not todo:
        print("Nothing new to process.")
        return 0

    print(f"Processing {len(todo)} new papers from {args.category}: {', '.join(todo)}\n")
    client = OpenAI()
    done = []
    for i, aid in enumerate(todo, 1):
        print(f"=== [{i}/{len(todo)}] {aid} ===")
        try:
            process_one(aid, client, args.voice)
            seen.add(aid)
            save_seen(seen)  # persist after each, so a crash doesn't lose progress
            done.append(aid)
        except Exception as e:  # one bad paper shouldn't kill the batch
            print(f"      !! skipped {aid}: {type(e).__name__}: {e}")

    print(f"\nDone. Added {len(done)}/{len(todo)} papers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
