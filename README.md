# paper2pod

arXiv ID → LLM-narrated MP3 → private podcast feed your phone can subscribe to.

```
python paper2pod.py 2605.28405
```

Outputs `audio/2605.28405.mp3` and appends an entry to `feed.xml`. Point Pocket Casts / Overcast / Apple Podcasts at the hosted `feed.xml` URL and new papers show up like any other show.

---

## 1. Install (ailab1 or laptop)

```bash
pip install -r requirements.txt
# system deps
sudo apt-get install -y ffmpeg espeak-ng     # espeak-ng is Kokoro's phonemizer
```

Kokoro auto-downloads the model on first run (~330 MB). It uses GPU if available, falls back to CPU.

## 2. Set environment

```bash
export ANTHROPIC_API_KEY=sk-...
export FEED_BASE_URL=https://<your-user>.github.io/paper-pod/audio
# optional overrides:
export LLM_MODEL=claude-sonnet-4-6        # or claude-opus-4-7 for higher quality
export KOKORO_VOICE=af_bella               # af_sky, am_adam, bf_emma, etc.
```

**Swap LLM provider** — replace the `Anthropic()` client in `generate_script()`. CosmosAI / OpenAI / vLLM all work; the prompt is provider-agnostic. For CosmosAI on ailab1 you'd point an `OpenAI(base_url=..., api_key=...)` client at the proxy endpoint and call `chat.completions.create(...)`.

## 3. Run on one paper

```bash
python paper2pod.py 2605.28405
```

You'll get:
```
scripts/2605.28405.txt    # the LLM-written script (edit + re-synth if you don't like it)
audio/2605.28405.mp3      # ~5 MB, ~8–10 min
feed.xml                  # RSS feed, item appended
```

Listen locally first with `mpv audio/2605.28405.mp3` to gut-check.

## 4. Put it on your phone

### Option A — public GitHub Pages repo (free, easiest)

```bash
gh repo create paper-pod --public --source=. --push
# enable Pages: Settings → Pages → Source = main, / (root)
```

Your feed lives at `https://<user>.github.io/paper-pod/feed.xml`. In Pocket Casts: **Profile → Files → Add a URL**, paste the feed URL. Same dialog exists in Overcast, AntennaPod, Apple Podcasts (`Library → Follow a Show by URL`).

The repo is public, so don't commit anything sensitive — the papers themselves are already public arXiv content, but be aware the feed is too.

### Option B — private (Cloudflare R2 + Tailscale, or PayPal-internal hosting)

If you'd rather not publish, host the MP3s in any private bucket and serve `feed.xml` over Tailscale-only access. The pipeline doesn't change — just point `FEED_BASE_URL` at the bucket and `scp` the outputs after each run.

## 5. Stream 10 papers a day (daily.py)

`daily.py` pulls the newest papers from a category, dedupes against everything it's already done (`seen.json`), and runs each through the pipeline.

```bash
python daily.py --category cs.AI --count 10
```

This is what backs `https://arxiv.org/list/cs.AI/recent` — same data, newest-first. Because cs.AI gets hundreds of submissions a day, the newest 10 will essentially always be 10 brand-new papers; `seen.json` just guarantees you never re-narrate one if you run it twice or there's day-to-day overlap.

Schedule it:

```bash
# crontab -e   (runs 7am daily)
0 7 * * * cd ~/paper-pod && \
  ANTHROPIC_API_KEY=sk-... FEED_BASE_URL=https://<user>.github.io/paper-pod/audio \
  /usr/bin/python daily.py --category cs.AI --count 10 >> daily.log 2>&1 && \
  git add audio scripts feed.xml seen.json && git commit -m "daily $(date +%F)" && git push
```

Notes:
- **Different feeds per topic:** run it twice with `--category cs.AI` and `--category cs.SD` (speech/audio — closer to your voice work). They share one `feed.xml`, or set a different `FEED_PATH`/repo per category if you want separate shows.
- **Cost/time per run:** 10 papers ≈ 10 LLM calls + 10 Kokoro synth passes. On a GPU box that's ~10–20 min and a few cents of Sonnet.
- **Match the announcement feed exactly:** if you'd rather track arXiv's daily *announcement* mailing (includes cross-lists, excludes mid-day submissions) instead of newest-by-submission, swap `recent_ids()` to parse `https://rss.arxiv.org/rss/cs.AI` — each `<item>`'s `<link>` is the abs URL. Filter on `<arxiv:announce_type>new</arxiv:announce_type>` to drop replacements and cross-listings.
- **Fewer/more per day:** just change `--count`.
- One bad paper (no HTML, weird PDF) is logged and skipped, not fatal.

## Tuning knobs

| Want | Edit |
|---|---|
| Longer / shorter episodes | word count in `SCRIPT_PROMPT` |
| Two-host dialogue style | rewrite `SCRIPT_PROMPT` to output `[HOST_A]` / `[HOST_B]` tags, then route alternating chunks through two Kokoro voices in `synthesize()` |
| Different voice domain framing | add a system message — "focus on TTS architecture, codec design, eval methodology" |
| Faster generation | `claude-haiku-4-5` for the script step; quality drop is noticeable but ~3× cheaper |
| Better TTS | swap Kokoro for a dialogue model (Dia, Sesame CSM) — keep the same chunk-and-concat structure |

## Known limitations

- arXiv HTML isn't available for every paper; PDF fallback works but loses section structure.
- Equation-heavy papers (theory-heavy ML) still produce awkward narration even with the prose-translation prompt — those are better read than listened to.
- Kokoro mispronounces rare proper nouns occasionally; you can pre-edit `scripts/<id>.txt` and re-run with `--skip-feed` after deleting the mp3 to re-synth without re-LLM'ing.
