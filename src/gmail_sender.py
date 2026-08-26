from __future__ import annotations

import base64
import html
import logging
import os
import re
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Optional

from src.config import get_data_dir
from src.summarizer import SummaryResult


log = logging.getLogger(__name__)


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _token_path() -> Path:
    return get_data_dir() / "gmail_token.json"


def _require_client_config() -> tuple[str, str]:
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env. "
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


def sender_address() -> str:
    addr = os.getenv("GMAIL_ADDRESS", "").strip()
    if not addr:
        raise ValueError("GMAIL_ADDRESS is not set in .env.")
    return addr


def recipients() -> list[str]:
    raw = os.getenv("WATCH_LATER_RECIPIENTS", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return [sender_address()]


def is_authenticated() -> bool:
    return _token_path().exists()


def run_local_auth(headless: bool = False) -> Path:
    """Run OAuth2 flow and persist the token. Returns the token file path."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_config(
        _client_config(),
        scopes=[GMAIL_SEND_SCOPE],
    )

    if headless:
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
        auth_url, _ = flow.authorization_url(
            prompt="consent", access_type="offline", include_granted_scopes="true"
        )
        print("\nOpen this URL in any browser, grant access, then paste the code below:\n")
        print(auth_url)
        print()
        code = input("Authorization code: ").strip()
        flow.fetch_token(code=code)
    else:
        flow.run_local_server(port=0, open_browser=True)

    credentials = flow.credentials
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    log.info("Saved Gmail token to %s", token_path)
    return token_path


def _load_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = _token_path()
    if not token_path.exists():
        raise FileNotFoundError(
            f"Gmail token not found at {token_path}. "
            f"Run: python -m src.cli gmail-auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), [GMAIL_SEND_SCOPE])

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            raise RuntimeError(
                f"Gmail token refresh failed: {e}. Re-run: python -m src.cli gmail-auth"
            ) from e

    if not creds.valid:
        raise RuntimeError(
            f"Gmail token is invalid. Re-run: python -m src.cli gmail-auth"
        )

    return creds


def _gmail_service():
    from googleapiclient.discovery import build

    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Markdown -> HTML conversion (intentionally minimal; covers the prompt output)
# ---------------------------------------------------------------------------

_HRULE_RE = re.compile(r"^\s*---+\s*$", re.MULTILINE)


def _md_to_html(md: str) -> str:
    """Convert the justdumpit markdown summary to a small HTML body.

    Handles: headings (#-####), bullet lists, fenced code, bold/italic,
    inline code, links, blockquotes, horizontal rules. Falls back to escaped
    preformatted text for anything we don't recognise so formatting survives.
    """
    if not md:
        return "<pre></pre>"

    text = md.replace("\r\n", "\n")
    out: list[str] = []
    in_code = False
    in_list: Optional[str] = None
    list_buf: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(paragraph).strip()
            if joined:
                out.append(f"<p>{_inline(joined)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal in_list
        if in_list and list_buf:
            tag = in_list
            out.append(f"<{tag}>")
            for item in list_buf:
                out.append(f"  <li>{_inline(item)}</li>")
            out.append(f"</{tag}>")
            list_buf.clear()
        in_list = None

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()

        if line.strip().startswith("```"):
            flush_paragraph()
            flush_list()
            if not in_code:
                out.append("<pre><code>")
                in_code = True
            else:
                out.append("</code></pre>")
                in_code = False
            continue
        if in_code:
            out.append(html.escape(line))
            continue

        if _HRULE_RE.match(line):
            flush_paragraph()
            flush_list()
            out.append("<hr>")
            continue

        if line.startswith("#"):
            flush_paragraph()
            flush_list()
            level = len(line) - len(line.lstrip("#"))
            level = max(1, min(6, level))
            heading = line[level:].strip()
            out.append(f"<h{level}>{_inline(heading)}</h{level}>")
            continue

        if line.startswith(">"):
            flush_paragraph()
            flush_list()
            out.append(f"<blockquote>{_inline(line.lstrip('>').strip())}</blockquote>")
            continue

        bullet_match = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            if in_list != "ul":
                flush_list()
                in_list = "ul"
            list_buf.append(bullet_match.group(1).strip())
            continue

        ordered_match = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if ordered_match:
            flush_paragraph()
            if in_list != "ol":
                flush_list()
                in_list = "ol"
            list_buf.append(ordered_match.group(1).strip())
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            continue

        paragraph.append(line.strip())

    flush_paragraph()
    flush_list()
    if in_code:
        out.append("</code></pre>")

    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)

    def link_sub(m: re.Match) -> str:
        label = m.group(1).strip()
        url = m.group(2).strip()
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def _html_shell(body_html: str, footer: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "max-width:780px;margin:0 auto;padding:16px;color:#222;line-height:1.5;}"
        "h1,h2,h3,h4{color:#111;line-height:1.25;margin-top:1.4em;}"
        "h1{font-size:1.5em;border-bottom:1px solid #eee;padding-bottom:.2em;}"
        "h2{font-size:1.25em;}"
        "h3{font-size:1.1em;}"
        "ul,ol{padding-left:1.4em;}"
        "li{margin:.2em 0;}"
        "code{background:#f4f4f4;padding:1px 4px;border-radius:3px;font-size:.92em;}"
        "pre{background:#f4f4f4;padding:10px;border-radius:6px;overflow-x:auto;}"
        "pre code{background:transparent;padding:0;}"
        "blockquote{border-left:3px solid #ccc;margin:.6em 0;padding:.1em 1em;color:#555;}"
        "hr{border:none;border-top:1px solid #eee;margin:1.4em 0;}"
        ".footer{margin-top:2em;padding-top:1em;border-top:1px solid #eee;color:#888;font-size:.85em;}"
        "</style></head><body>"
        f"{body_html}"
        f"<div class='footer'>{footer}</div>"
        "</body></html>"
    )


def _build_email(result: SummaryResult, from_addr: str, to_addrs: list[str]) -> EmailMessage:
    md = result.markdown or _fallback_markdown(result)
    html_body = _md_to_html(md)
    model = getattr(result, "model", "") or ""
    footer = (
        f"justdumpit · video_id={result.video_id} · "
        f"prompt={result.prompt_version}"
        + (f" · model={model}" if model else "")
    )
    html_doc = _html_shell(html_body, footer)

    msg = EmailMessage()
    subject = f"[Watch Later] {result.channel_name or 'YouTube'} — {result.video_title}"
    msg["Subject"] = subject[:998]
    msg["From"] = formataddr(("justdumpit", from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(md)
    msg.add_alternative(html_doc, subtype="html")
    return msg


def _fallback_markdown(result: SummaryResult) -> str:
    parts: list[str] = [f"# {result.video_title}", ""]
    if result.video_url:
        parts.append(f"URL: {result.video_url}")
    if result.channel_name:
        parts.append(f"Channel: {result.channel_name}")
    parts.append("")
    if result.tldr:
        parts += ["## TL;DR", "", result.tldr, ""]
    if result.argument:
        parts += ["## Argument", "", result.argument, ""]
    return "\n".join(parts)


def send_analysis_email(
    result: SummaryResult,
    to: Optional[list[str]] = None,
    from_addr: Optional[str] = None,
) -> dict:
    """Build and send the analysis email. Returns {message_id, thread_id, to}."""
    if not result.success:
        raise ValueError(f"Refusing to email failed analysis: {result.error}")

    from_addr = from_addr or sender_address()
    to_addrs = to or recipients()
    msg = _build_email(result, from_addr, to_addrs)

    service = _gmail_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    sent = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )
    log.info(
        "Sent Watch Later email for %s (%s) to %s",
        result.video_id,
        sent.get("id"),
        ", ".join(to_addrs),
    )
    return {
        "message_id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "to": to_addrs,
        "from": from_addr,
        "subject": msg["Subject"],
    }
