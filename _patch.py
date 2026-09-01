"""Patch /status endpoint to include OAuth + cookie freshness."""
import sys
from pathlib import Path

p = Path(sys.argv[1])
content = p.read_text(encoding="utf-8")

OLD = '''@app.get("/status")
def status():
    from datetime import datetime, timezone
    from src.config import get_data_dir
    from src.transcript import _resolve_cookies_file

    cookies_file = _resolve_cookies_file()
    cookies_loaded = cookies_file is not None
    cookies_path = str(cookies_file) if cookies_file else None

    recent_blocks = db.get_recent_failures(reason="ip_blocked", since_hours=24)
    recent_all = db.get_recent_failures(since_hours=1)
    last_block = recent_blocks[0]["happened_at"] if recent_blocks else None

    youtube = {
        "reachable": len(recent_blocks) == 0,
        "cookies_loaded": cookies_loaded,
        "cookies_path": cookies_path,
        "last_blocked_at": last_block,
        "recent_block_count_24h": len(recent_blocks),
        "recent_failure_count_1h": len(recent_all),
        "cookies_needed": len(recent_blocks) > 0 or not cookies_loaded,
    }
    return {
        "ok": True,
        "youtube": youtube,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }'''

NEW = '''@app.get("/status")
def status():
    from datetime import datetime, timezone
    from src.config import get_data_dir
    from src.transcript import _resolve_cookies_file
    from src.youtube_watch_later import _token_path, _load_credentials

    cookies_file = _resolve_cookies_file()
    cookies_loaded = cookies_file is not None
    cookies_path = str(cookies_file) if cookies_file else None
    cookies_age_hours = None
    if cookies_file and cookies_file.exists():
        mtime = cookies_file.stat().st_mtime
        cookies_age_hours = round((datetime.now().timestamp() - mtime) / 3600, 1)

    token_path = _token_path()
    oauth = {"path": str(token_path), "exists": token_path.exists()}
    if token_path.exists():
        try:
            creds = _load_credentials()
            now = datetime.now(timezone.utc)
            exp = creds.expiry
            expires_in_hours = round((exp - now).total_seconds() / 3600, 1) if exp else None
            oauth["scope"] = list(creds.scopes) if creds.scopes else None
            oauth["valid"] = creds.valid
            oauth["expired"] = creds.expired
            oauth["has_refresh_token"] = bool(creds.refresh_token)
            oauth["expires_at"] = exp.isoformat() if exp else None
            oauth["expires_in_hours"] = expires_in_hours
        except Exception as e:
            oauth["error"] = str(e)

    recent_blocks = db.get_recent_failures(reason="ip_blocked", since_hours=24)
    recent_all = db.get_recent_failures(since_hours=1)
    last_block = recent_blocks[0]["happened_at"] if recent_blocks else None

    youtube = {
        "reachable": len(recent_blocks) == 0,
        "cookies_loaded": cookies_loaded,
        "cookies_path": cookies_path,
        "cookies_age_hours": cookies_age_hours,
        "last_blocked_at": last_block,
        "recent_block_count_24h": len(recent_blocks),
        "recent_failure_count_1h": len(recent_all),
        "cookies_needed": len(recent_blocks) > 0 or not cookies_loaded or (cookies_age_hours or 0) > 48,
    }
    return {
        "ok": True,
        "youtube": youtube,
        "oauth": oauth,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }'''

if OLD not in content:
    print("OLD NOT FOUND")
    sys.exit(1)
p.write_text(content.replace(OLD, NEW, 1), encoding="utf-8")
print("DONE")
