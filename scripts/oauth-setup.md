# Watch Later + Gmail OAuth2 setup

The Watch Later pipeline uses two Google OAuth2 clients (YouTube + Gmail). This guide covers one-time setup, both locally and on Fly.io.

## Why OAuth2 and not API keys

- **YouTube "Watch Later" is a user-private playlist** — it can only be read with the user's own OAuth credentials. An API key won't work.
- **Gmail sending** — to send as your address you must authenticate as that user. OAuth2 is the only well-supported path.

Both clients are **read or send only** (no destructive scopes). You can revoke access at any time from [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

---

## 1. Create the OAuth2 clients in Google Cloud

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services** → **OAuth consent screen**
   - User type: **External** (or Internal if you're in a Workspace org)
   - App name: `justdumpit`
   - Scopes: add `youtube.readonly` and `gmail.send`
   - Test users: add your Gmail address (required while the app is in "Testing")
   - Save

2. **Enable the APIs** (Library):
   - YouTube Data API v3
   - Gmail API

3. **Create credentials** → OAuth client ID:
   - Application type: **Desktop app**
   - Name: `justdumpit-youtube` (then repeat for `justdumpit-gmail`)
   - Two separate clients are recommended so you can revoke one independently.
   - Download each JSON; you'll need `client_id` and `client_secret`.

> The Google Cloud wizard offers a "Desktop app" template — use it. Do NOT use "Web application" unless you're intentionally setting up a deployed callback URL.

---

## 2. Local first-run auth

Edit your local `.env`:

```env
YOUTUBE_CLIENT_ID=...apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=GOCSPX-...
GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=GOCSPX-...
GMAIL_ADDRESS=you@gmail.com
```

Then:

```powershell
# YouTube (scope: youtube.readonly)
python -m src.cli watch-later-auth

# Gmail (scope: gmail.send)
python -m src.cli gmail-auth
```

A browser tab opens, you grant the scope, and the token JSON is saved to:
- `./data/youtube_token.json`
- `./data/gmail_token.json`

Both are in `.gitignore`. Tokens auto-refresh from the refresh_token until revoked.

### Headless / SSH / CI mode

If the local machine has no browser (or you're inside a container), use:

```powershell
python -m src.cli watch-later-auth --headless
python -m src.cli gmail-auth --headless
```

You'll get a URL. Open it in any browser, grant access, paste the code back into the terminal.

---

## 3. Local smoke test

```powershell
python -m src.cli watch-later-status
```

Should show `youtube_authenticated: true` and `gmail_authenticated: true`.

Add a video to your YouTube "Watch Later" list, then:

```powershell
python -m src.cli watch-later-sync
```

You should get the analysis emailed within ~1 minute. Check your Gmail inbox (or `python -m src.cli watch-later-list` to see DB state).

---

## 4. Fly.io deployment

### 4a. Set secrets

```powershell
fly secrets set `
  YOUTUBE_CLIENT_ID=... `
  YOUTUBE_CLIENT_SECRET=... `
  GMAIL_CLIENT_ID=... `
  GMAIL_CLIENT_SECRET=... `
  GMAIL_ADDRESS=you@gmail.com
```

### 4b. Upload tokens to the persistent volume

The Fly app has a 1GB volume mounted at `/data`. Token files must live there. Easiest path:

```powershell
# Copy tokens to the volume via sftp
fly ssh sftp shell
> put data/youtube_token.json /data/youtube_token.json
> put data/gmail_token.json /data/gmail_token.json
> chmod 600 /data/youtube_token.json /data/gmail_token.json
> exit
```

(The `fly.toml` already mounts `ytscraper_data` at `/data`.)

### 4c. Restart the app

```powershell
fly deploy
```

### 4d. Verify

```powershell
fly ssh console -C "python -m src.cli watch-later-status"
```

Or hit the API:

```
GET https://justdumpit-ytscraper.fly.dev/watch-later/status
```

### 4e. Manual sync trigger (for testing)

```
POST https://justdumpit-ytscraper.fly.dev/watch-later/sync?limit=5
```

The next scheduled poll runs automatically every `WATCH_LATER_POLL_INTERVAL` seconds (default 3600 = 1h).

---

## 5. Token rotation / re-auth

If a token stops working (revoked, password changed, scope changed):

```powershell
# Locally:
python -m src.cli watch-later-auth   # overwrites ./data/youtube_token.json
python -m src.cli gmail-auth         # overwrites ./data/gmail_token.json

# Then re-upload to Fly:
fly ssh sftp shell
> put data/youtube_token.json /data/youtube_token.json
> put data/gmail_token.json /data/gmail_token.json
> exit

fly machine restart <machine-id>     # or just fly deploy
```

To revoke entirely: visit [myaccount.google.com/permissions](https://myaccount.google.com/permissions) → remove `justdumpit`.

---

## 6. Multi-recipient (optional)

By default, emails go to the same Gmail account that sent them (you). To send to additional recipients:

```env
WATCH_LATER_RECIPIENTS=you@gmail.com,agent@gmail.com,team@example.com
```

The first address must still be the authenticated `GMAIL_ADDRESS`; additional addresses receive as BCC-less To-recipients.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `400 invalid_client` on auth URL | Wrong `YOUTUBE_CLIENT_ID`/`SECRET` or app is "Web application" instead of "Desktop" | Re-create credential as **Desktop app** |
| `403 access_denied` | App in "Testing" mode and your email isn't a test user | Add the email under OAuth consent screen → Test users |
| `403 insufficient authentication scopes` | Token was issued before the scope was added | Delete the token JSON, re-run `*-auth` |
| `Watch Later fetch error: youtube_token.json not found` | Tokens not on the volume | Re-run step 4b |
| `Email send failed: 403-... gmail.send` | Gmail API not enabled, or token lacks scope | Enable Gmail API in Cloud Console, re-auth |
| No new videos detected even though WL has items | The video is already in `watch_later_processed` table | Check `python -m src.cli watch-later-list`; delete the row to re-process |
