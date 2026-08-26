# YTTranscriptScraper

A personal YouTube knowledge harvester. Take a video URL or follow a list of channels; the tool fetches transcripts, runs structured LLM extraction, and persists everything to a local SQLite knowledge base with semantic search over chunks. Designed to run on Oracle Cloud Always Free (free forever) so it's reachable from any device.

## What it does

For each video:

1. **Acquire** — pulls the transcript via `youtube-transcript-api` (multi-language fallback)
2. **Extract** — chunked map pass extracts `claims`, `concepts`, `examples`, `actions`, `entities` per chunk via your LLM
3. **Synthesise** — reduce pass produces `tldr`, `argument`, deduplicated `key_concepts`, grouped `takeaways`, `claims_to_verify`, `glossary`, plus rendered `markdown`
4. **Store** — videos, transcripts, analyses (keyed by prompt version), chunks with embeddings in SQLite
5. **Search** — semantic search over all chunks via `all-MiniLM-L6-v2` embeddings

For watched channels, RSS polling discovers new videos automatically every 6 hours (via systemd timer on the VM).

**Watch Later pipeline:** OAuth-authenticate against your Google account once, and every time you save a YouTube video for later it gets fetched, analysed, and emailed to your Gmail so a downstream LLM agent can decide if it's useful for any of your other GitHub repos or sparks a new project idea. See `scripts/oauth-setup.md` for setup.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and add your MINIMAX_API_KEY
```

Optional env vars (all read at runtime):
- `MINIMAX_API_KEY` — required
- `YTSCRAPER_DB_PATH` — defaults to `./kb.db`
- `HF_HOME` / `SENTENCE_TRANSFORMERS_HOME` — embedding model cache
- Watch Later / Gmail OAuth2: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_ADDRESS`

## CLI

```bash
python -m src.cli analyse <url>          # one-off URL, full pipeline
python -m src.cli add-channel <url>      # add a channel to watch
python -m src.cli channels               # list watched channels
python -m src.cli sync                   # poll RSS, ingest new videos
python -m src.cli sync --dry-run         # preview what would be ingested
python -m src.cli backfill <url> --limit 5  # catch-up ingest N most recent
python -m src.cli search "<query>"       # semantic search the KB
python -m src.cli videos # list ingested videos
python -m src.cli db                     # KB stats
python -m src.cli server --port 8765     # FastAPI + web UI
python -m src.cli backup                 # snapshot KB to backups/

# Watch Later pipeline
python -m src.cli watch-later-auth       # OAuth: YouTube (browser)
python -m src.cli gmail-auth             # OAuth: Gmail (browser)
python -m src.cli watch-later-status     # show loop / auth / DB state
python -m src.cli watch-later-sync       # one-shot poll + process + email
python -m src.cli watch-later-list       # show entries we've recorded
```

## Web UI

`python -m src.cli server` starts FastAPI on `127.0.0.1:8765` with a mobile-friendly UI at `/`. Search, add a one-off URL, browse videos and channels — all from your phone.

## Deploy to Oracle Cloud Always Free

Free VM, free forever. See `scripts/dns-setup.md` for the domain/DNS walkthrough, then `scripts/oracle-setup.sh` for the one-shot setup. The script installs Python, Caddy, the systemd units (`ytscraper-api`, `ytscraper-sync.timer`, `ytscraper-backup.timer`), and configures HTTPS via Let's Encrypt.

```bash
# On your Oracle VM (Ubuntu 22.04/24.04 ARM):
export YTSCRAPER_DOMAIN=justdumpit.online
export MINIMAX_API_KEY='your-key'
sudo -E bash scripts/oracle-setup.sh
```

That's it. The API will be live at `https://justdumpit.online` within a minute.

## Deploy to Fly.io

Simpler than Oracle — `fly deploy` from Windows, persistent volume for `kb.db`. See `scripts/fly-deploy.md` for the full walkthrough.

```powershell
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup
.\scripts\deploy.ps1
```

Live at `https://justdumpit-ytscraper.fly.dev`. Attach `justdumpit.online` via `fly certs create justdumpit.online` after pointing DNS at Fly's IPs.

## Architecture

| Stage | Component | Where |
|---|---|---|
| Acquire | youtube-transcript-api tier | `src/transcript.py` |
| Extract | map pass per chunk (LLM) | `src/summarizer.py` + `src/prompts/v1/map.txt` |
| Synthesise | reduce pass (LLM) | `src/summarizer.py` + `src/prompts/v1/reduce.txt` |
| Embed | all-MiniLM-L6-v2 | `src/embeddings.py` |
| Store | SQLite at `kb.db` | `src/schema.sql`, `src/db.py` |
| Auto-ingest | RSS via feedparser, systemd timer | `src/feeds.py`, `src/scheduler.py` |
| Serve | FastAPI + Caddy TLS | `src/server.py`, `scripts/Caddyfile` |

## Prompt versioning

Prompts live in `src/prompts/<version>/{map,reduce}.txt`. Analyses are keyed on `(video_id, prompt_version)` so iterating on a prompt doesn't refetch transcripts. To bump the version: copy `v1/` to `v2/`, edit the new files, and re-run `analyse` — old analyses remain intact.

## Layout

```
src/
  cli.py            # CLI entry
  server.py         # FastAPI app
  schema.sql        # DB schema
  db.py             # connection + CRUD
  channel.py        # yt-dlp playlist listing (legacy batch flow)
  feeds.py          # RSS poller
  registry.py       # channels.json
  scheduler.py      # sync logic
  summarizer.py     # map/reduce LLM pipeline
  chunker.py        # transcript chunking
  embeddings.py     # sentence-transformers wrapper
  search.py         # semantic search over chunks
  transcript.py     # transcript + metadata fetch
  output.py         # JSON+MD file writer
  config.py         # settings
  prompts/
    v1/
      map.txt
      reduce.txt
  static/
    index.html      # mobile web UI
scripts/
  oracle-setup.sh
  Caddyfile
  ytscraper-api.service
  ytscraper-sync.{service,timer}
  ytscraper-backup.{service,timer}
  dns-setup.md
channels.json       # watched channels registry
kb.db               # SQLite knowledge base
backups/            # nightly DB snapshots (created on demand)
output/             # per-video JSON+MD files (human artefacts)
```