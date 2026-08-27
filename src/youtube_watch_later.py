from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import get_data_dir


log = logging.getLogger(__name__)


YOUTUBE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/youtube",
]
YOUTUBE_LOGIN_HINT = os.getenv("YOUTUBE_LOGIN_HINT", "").strip()

WATCH_LATER_PLAYLIST_ID = "WL"


@dataclass
class WatchLaterEntry:
    video_id: str
    title: str
    channel_title: str
    channel_id: str
    video_url: str
    added_to_watch_later_at: str
    thumbnail_url: Optional[str] = None


@dataclass
class WatchLaterFetchResult:
    entries: list[WatchLaterEntry]
    next_page_token: Optional[str]
    error: Optional[str] = None


def _token_path() -> Path:
    return get_data_dir() / "youtube_token.json"


def _require_client_config() -> tuple[str, str]:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env. "
            "Create a Desktop-app OAuth2 client at "
            "https://console.cloud.google.com/apis/credentials and add the values."
        )
    return client_id, client_secret


def _client_config() -> dict:
    client_id, client_secret = _require_client_config()
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }


def is_authenticated() -> bool:
    return _token_path().exists()


def run_local_auth(headless: bool = False) -> Path:
    """Run OAuth2 flow and persist the token. Returns the token file path."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_config(
        _client_config(),
        scopes=YOUTUBE_SCOPES,
        autogenerate_code_verifier=True,
    )

    if headless:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlparse, parse_qs
        import threading as _threading
        import time as _time

        code_file = os.getenv("OAUTH_CODE_FILE", "").strip()
        pre_code: Optional[str] = None
        if code_file and Path(code_file).exists():
            try:
                pre_code = Path(code_file).read_text(encoding="utf-8").strip() or None
            except OSError:
                pre_code = None

        if pre_code:
            print(f"[auth] Using code from {code_file}", flush=True)
            flow.fetch_token(code=pre_code)
        else:
            port = int(os.getenv("OAUTH_LOCAL_PORT", "18099"))
            flow.redirect_uri = f"http://127.0.0.1:{port}"

            captured: dict[str, Optional[str]] = {"code": None, "error": None}
            event = _threading.Event()

            class _Handler(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    parsed = urlparse(self.path)
                    qs = parse_qs(parsed.query)
                    if "code" in qs:
                        captured["code"] = qs["code"][0]
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(
                            b"<html><body><h2>Authorised. You can close this tab.</h2>"
                            b"<p>Return to your terminal.</p></body></html>"
                        )
                    elif "error" in qs:
                        captured["error"] = qs["error"][0]
                        self.send_response(400)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(
                            f"<html><body><h2>Error: {captured['error']}</h2></body></html>".encode()
                        )
                    else:
                        self.send_response(404)
                        self.end_headers()
                    event.set()

                def log_message(self, fmt, *args):  # silence
                    pass

            server = HTTPServer(("127.0.0.1", port), _Handler)
            thread = _threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            extra_url_params: dict[str, str] = {
                "prompt": "select_account consent",
                "access_type": "offline",
                "include_granted_scopes": "false",
            }
            if YOUTUBE_LOGIN_HINT:
                extra_url_params["login_hint"] = YOUTUBE_LOGIN_HINT

            auth_url, _ = flow.authorization_url(**extra_url_params)
            print("\nOpen this URL in ANY browser, sign in, and grant access:\n")
            print(auth_url, flush=True)
            print(f"\nListening on http://127.0.0.1:{port} for the callback...", flush=True)
            print("(You'll see 'site not reachable' after clicking Allow - that's fine, the auth is already captured.)")

            try:
                event.wait(timeout=600)
            finally:
                server.shutdown()

            if captured["error"]:
                raise RuntimeError(f"OAuth error: {captured['error']}")
            if not captured["code"]:
                raise RuntimeError("Timed out waiting for OAuth callback (no code received)")
            flow.fetch_token(code=captured["code"])
    else:
        flow.run_local_server(port=0, open_browser=True)

    credentials = flow.credentials
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    log.info("Saved YouTube token to %s", token_path)
    return token_path


def _load_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = _token_path()
    if not token_path.exists():
        raise FileNotFoundError(
            f"YouTube token not found at {token_path}. "
            f"Run: python -m src.cli watch-later-auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            raise RuntimeError(
                f"YouTube token refresh failed: {e}. Re-run: python -m src.cli watch-later-auth"
            ) from e

    if not creds.valid:
        raise RuntimeError(
            f"YouTube token is invalid. Re-run: python -m src.cli watch-later-auth"
        )

    return creds


def _build_youtube_client():
    from googleapiclient.discovery import build

    creds = _load_credentials()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def fetch_watch_later(page_token: Optional[str] = None, max_results: int = 50) -> WatchLaterFetchResult:
    """Fetch a page of Watch Later entries. Caller iterates with next_page_token."""
    try:
        youtube = _build_youtube_client()
    except Exception as e:
        return WatchLaterFetchResult(entries=[], next_page_token=None, error=str(e))

    try:
        response = (
            youtube.playlistItems()
            .list(
                part="snippet,contentDetails",
                playlistId=WATCH_LATER_PLAYLIST_ID,
                maxResults=max(1, min(max_results, 50)),
                pageToken=page_token,
            )
            .execute()
        )
    except Exception as e:
        return WatchLaterFetchResult(entries=[], next_page_token=page_token, error=str(e))

    entries: list[WatchLaterEntry] = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {})
        resource = snippet.get("resourceId", {})
        video_id = resource.get("videoId")
        if not video_id or len(video_id) != 11:
            continue

        title = snippet.get("title", "")
        channel_title = snippet.get("channelTitle", "")
        channel_id = snippet.get("channelId", "")
        added_raw = snippet.get("publishedAt", "")
        thumbnails = snippet.get("thumbnails", {}) or {}
        thumb = (
            thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or thumbnails.get("default", {}).get("url")
        )

        entries.append(
            WatchLaterEntry(
                video_id=video_id,
                title=title,
                channel_title=channel_title,
                channel_id=channel_id,
                video_url=f"https://www.youtube.com/watch?v={video_id}",
                added_to_watch_later_at=_normalize_iso(added_raw),
                thumbnail_url=thumb,
            )
        )

    return WatchLaterFetchResult(
        entries=entries,
        next_page_token=response.get("nextPageToken"),
    )


def fetch_all_watch_later() -> WatchLaterFetchResult:
    """Fetch all Watch Later entries, paging through the playlist."""
    all_entries: list[WatchLaterEntry] = []
    page_token: Optional[str] = None
    last_error: Optional[str] = None

    for _ in range(20):
        result = fetch_watch_later(page_token=page_token, max_results=50)
        if result.error:
            last_error = result.error
            break
        all_entries.extend(result.entries)
        page_token = result.next_page_token
        if not page_token:
            break

    return WatchLaterFetchResult(
        entries=all_entries,
        next_page_token=None,
        error=last_error,
    )


def _normalize_iso(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return value


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
