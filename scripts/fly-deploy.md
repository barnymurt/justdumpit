# Deploy to Fly.io

Live at `https://justdumpit-ytscraper.fly.dev` (or attach `justdumpit.online` once DNS points at Fly).

## Why Fly.io

- Persistent volume for `kb.db` + HuggingFace cache (survives deploys)
- Built-in HTTPS via Let's Encrypt
- Auto-restart on crash, health checks, free tier (free allowance covers small workloads; ML model loading requires ≥1GB RAM which is paid — ~$2/mo)
- `fly deploy` from Windows → live in ~2 minutes

## One-time setup

1. **Install flyctl** (Windows PowerShell):
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   ```
   Restart your terminal so `fly` is on PATH.

2. **Sign up** (card required for free tier, you won't be charged for the free allowance):
   ```powershell
   fly auth signup
   ```

3. **Point justdumpit.online at Fly** — at your registrar, add:
   ```
   A     @     <fly-v4-ipv4>      Auto
   AAAA  @     <fly-v6-ipv6>      Auto
   ```
   Get the IPs from `fly ips list` once the app is created, or just use the auto-assigned `justdumpit-ytscraper.fly.dev` first and add the custom domain later:
   ```powershell
   fly certs create justdumpit.online
   fly certs create www.justdumpit.online
   ```

## First deploy

From the project directory:
```powershell
.\scripts\deploy.ps1
```

This will:
- Verify `fly auth`
- Create the app `justdumpit-ytscraper` if it doesn't exist
- Create a 1GB persistent volume named `ytscraper_data` in `lhr` (London)
- Push secrets from your local `.env`
- Build the image on Fly's build servers and deploy

After deploy, `https://justdumpit-ytscraper.fly.dev` is live.

## Subsequent deploys

```powershell
.\scripts\deploy.ps1
```

That's it. `fly deploy` only rebuilds layers that changed.

## Where state lives

Everything that needs to survive deploys is mounted at `/data` on the volume `ytscraper_data`:

| Path | What |
|---|---|
| `/data/kb.db` | SQLite knowledge base |
| `/data/channels.json` | Watched channels |
| `/data/output/` | Per-video JSON + Markdown artefacts |
| `/data/backups/` | Nightly backup snapshots (from `fly ssh` if you wire one) |
| `/data/.cache/huggingface` | Embedding model — cached so we don't redownload ~80MB each deploy |

## Day-to-day commands

```powershell
fly logs                                       # tail app logs
fly ssh console                                # shell into the running container
fly ssh console -C "python -m src.cli stats"   # one-off command on the running machine
fly secrets set MINIMAX_API_KEY=newkey         # rotate secrets
fly volumes list                               # see the persistent volume
fly scale memory 2048                          # bump RAM (paid)
```

## Local dev (unchanged)

```powershell
python -m src.cli server --port 8765
```

Local state goes to `./data/` (was previously `./`). The legacy files at `./kb.db`, `./channels.json`, `./output/`, `./backups/` are auto-migrated on first run.

## Notes / trade-offs

- **Fly's free allowance** is 3 shared VMs with 256MB RAM each — too tight for sentence-transformers. The `fly.toml` requests 1GB shared, which is paid (~$2/mo). If you want to keep it free, use a smaller embedding model or offload embeddings to an external API.
- **No multi-region / no horizontal scale** for free. Personal use = single region (lhr) is fine.
- **Background sync** runs as a thread inside the web process — simpler than a separate Fly machine. Set `YTSCRAPER_DISABLE_SCHEDULER=1` to turn it off (e.g. if you later add a cron machine).