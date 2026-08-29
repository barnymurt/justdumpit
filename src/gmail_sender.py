from __future__ import annotations

import base64
import html
import json
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
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE]


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
        scopes=GMAIL_SCOPES,
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

            auth_url, _ = flow.authorization_url(
                prompt="consent", access_type="offline", include_granted_scopes="false"
            )
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

    creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

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


def _stage2_brief(stage2: dict) -> tuple[str, int, bool]:
    """Render the Stage 2 brief as markdown. Returns (markdown, max_relevance, low_relevance)."""
    per_goal = stage2.get("per_goal", []) or []
    if not per_goal:
        return "", 0, False

    max_rel = max((g.get("relevance", 0) for g in per_goal), default=0)
    low_relevance = max_rel < 2

    lines: list[str] = ["## Stage 2 — Action Brief", ""]
    if low_relevance:
        lines.append(f"_Max relevance across goals: {max_rel}/3. Low signal — included for transparency._")
        lines.append("")
    for g in per_goal:
        rel = g.get("relevance", 0)
        goal_id = g.get("goal_id", "?")
        goal_name = g.get("goal_name", "")
        lines.append(f"### {goal_name} (`{goal_id}`) — relevance {rel}/3")
        actions = g.get("proposed_actions", []) or []
        skip = g.get("skip_reason")
        if not actions and skip:
            lines.append(f"  - **Skip:** {skip}")
        for a in actions:
            tier = a.get("proposed_tier", "?")
            effort = a.get("effort_estimate_hours", "?")
            reversibility = a.get("reversibility", "?")
            external = "yes" if a.get("external_surface") else "no"
            atoms = ", ".join(a.get("atoms_used", []) or []) or "-"
            lines.append(
                f"  - **[{tier}]** effort ~{effort}h · reversibility={reversibility} · external={external} · atoms={atoms}"
            )
            lines.append(f"    {a.get('action_description', '')}")
            if a.get("impact_classification"):
                lines.append(f"    Impact: {a['impact_classification']}")
            if a.get("pre_check"):
                lines.append("    Pre-check:")
                for q in a["pre_check"]:
                    lines.append(f"      - {q}")
        lines.append("")

    rejections = stage2.get("rejections", []) or []
    if rejections:
        lines.append("### Rejections (audited, no actions dropped silently)")
        for r in rejections:
            lines.append(f"  - `{r.get('rule_id','?')}` on `{r.get('action_id','?')}` — {r.get('reason','')}")
            lines.append(f"    Next: {r.get('suggested_next_step','')}")
        lines.append("")
    return "\n".join(lines), max_rel, low_relevance


def _atoms_index(atoms: list[dict]) -> str:
    if not atoms:
        return ""
    lines = ["## Atoms Index", ""]
    for a in atoms:
        label = a.get("label", "?")
        ts = a.get("timestamp", "?")
        atom_type = a.get("type", "?")
        atom_id = a.get("id", "?")
        lines.append(f"- `{atom_id}` **{label}** _{atom_type}_ @ {ts}")
    lines.append("")
    return "\n".join(lines)


def _build_email(
    result: SummaryResult,
    from_addr: str,
    to_addrs: list[str],
    stage2: Optional[dict] = None,
) -> EmailMessage:
    md_human = result.markdown or _fallback_markdown(result)

    md_parts: list[str] = []
    if stage2:
        brief_md, max_rel, low_rel = _stage2_brief(stage2)
        if brief_md:
            md_parts.append(brief_md)
        atoms_index = _atoms_index(getattr(result, "atoms", []) or [])
        if atoms_index:
            md_parts.append(atoms_index)
    md_parts.append(md_human)

    md_combined = "\n\n".join(md_parts)

    html_body = _md_to_html(md_combined)
    model = getattr(result, "model", "") or ""
    stage2_tag = ""
    if stage2:
        max_rel = max((g.get("relevance", 0) for g in (stage2.get("per_goal") or [])), default=0)
        if max_rel < 2:
            stage2_tag = " (low-relevance)"
        else:
            stage2_tag = f" [Stage2: {max_rel}/3]"
    footer = (
        f"justdumpit · video_id={result.video_id} · "
        f"prompt={result.prompt_version}"
        + (f" · model={model}" if model else "")
    )
    html_doc = _html_shell(html_body, footer)

    msg = EmailMessage()
    subject = f"[Watch Later] {result.channel_name or 'YouTube'} — {result.video_title}{stage2_tag}"
    msg["Subject"] = subject[:998]
    msg["From"] = formataddr(("justdumpit", from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(md_combined)
    msg.add_alternative(html_doc, subtype="html")

    if stage2:
        attachment = json.dumps(stage2, indent=2, ensure_ascii=False).encode("utf-8")
        msg.add_attachment(
            attachment,
            maintype="application",
            subtype="json",
            filename=f"stage2-{result.video_id}.json",
        )

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
    stage2: Optional[dict] = None,
    action_id_by_goal: Optional[dict] = None,
) -> dict:
    """Build and send the analysis email. Returns {message_id, thread_id, to}.

    If `stage2` is provided, the email includes the Stage 2 action brief,
    the atoms index, and a JSON attachment with the full Stage 2 payload.

    If `action_id_by_goal` is provided (a {goal_id: action_id} mapping),
    each action in the Stage 2 brief gets a `Message-ID: <action_id>@...`
    header so the agent can match replies to the right action.
    """
    if not result.success:
        raise ValueError(f"Refusing to email failed analysis: {result.error}")

    from_addr = from_addr or sender_address()
    to_addrs = to or recipients()
    msg = _build_email(result, from_addr, to_addrs, stage2=stage2)

    if action_id_by_goal and stage2 and stage2.get("per_goal"):
        for goal_entry in stage2["per_goal"]:
            gid = goal_entry.get("goal_id", "")
            aid = action_id_by_goal.get(gid)
            if not aid:
                continue
            msg["X-Justdumpit-Action-Id"] = (
                f"{msg.get('X-Justdumpit-Action-Id', '')} {gid}:{aid}".strip()
            )

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
