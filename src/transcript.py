import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


log = logging.getLogger("ytscraper.transcript")


class TranscriptError(Exception):
    pass


@dataclass
class TranscriptResult:
    video_id: str
    video_title: str
    text: str
    available: bool
    error: Optional[str] = None
    segments: list[dict] = field(default_factory=list)
    tier: str = ""
    reason: str = ""           # '' on success; otherwise: 'ip_blocked' | 'no_captions' | 'unavailable' | 'rate_limited' | 'unknown'
    cookies_needed: bool = False


@dataclass
class VideoMetadata:
    video_id: str
    url: str
    title: str
    channel_id: Optional[str]
    channel_name: Optional[str]
    duration: Optional[int]
    published_at: Optional[str]
    available: bool = True
    error: Optional[str] = None


def get_video_id(url: str) -> str:
    match = re.search(r'(?:v=|/v/|youtu\.be/)([\w-]{11})', url)
    return match.group(1) if match else "unknown"


def get_video_metadata(url: str, verbose: bool = False) -> VideoMetadata:
    video_id = get_video_id(url)

    if verbose:
        print(f"Fetching metadata for: {url} (ID: {video_id})")

    try:
        from yt_dlp import YoutubeDL

        ydl_opts = {"quiet": True, "skip_download": True}
        cookies_file = _resolve_cookies_file()
        if cookies_file:
            ydl_opts["cookiefile"] = str(cookies_file)

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_id, download=False)

        upload_date = info.get('upload_date')
        published_at = None
        if upload_date and len(upload_date) == 8 and upload_date.isdigit():
            published_at = f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"

        duration = info.get('duration')
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = None

        channel_id = info.get('channel_id') or info.get('uploader_id')
        channel_name = info.get('channel') or info.get('uploader') or 'Unknown'

        return VideoMetadata(
            video_id=video_id,
            url=url if url.startswith('http') else f"https://www.youtube.com/watch?v={video_id}",
            title=str(info.get('title', 'Unknown')),
            channel_id=channel_id,
            channel_name=channel_name,
            duration=duration,
            published_at=published_at,
            available=True,
        )
    except Exception as e:
        return VideoMetadata(
            video_id=video_id,
            url=url if url.startswith('http') else f"https://www.youtube.com/watch?v={video_id}",
            title='Unknown',
            channel_id=None,
            channel_name=None,
            duration=None,
            published_at=None,
            available=False,
            error=str(e)[:200],
        )


def _resolve_cookies_file() -> Optional[Path]:
    """Find a Netscape cookies.txt for YouTube auth. Search order:
    1. $YTSCRAPER_COOKIES_FILE (absolute path)
    2. <data_dir>/youtube_cookies.txt
    3. $YOUTUBE_COOKIES_FILE
    """
    from src.config import get_data_dir

    candidates = []
    env_path = os.getenv("YTSCRAPER_COOKIES_FILE")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(get_data_dir() / "youtube_cookies.txt")
    env_path2 = os.getenv("YOUTUBE_COOKIES_FILE")
    if env_path2:
        candidates.append(Path(env_path2))

    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _classify_ytdlp_error(msg: str) -> Optional[str]:
    """Return a reason code if this yt-dlp error looks like an IP/auth block, else None."""
    lower = msg.lower()
    if "sign in to confirm" in lower or "not a bot" in lower:
        return "ip_blocked"
    if "429" in lower or "too many requests" in lower:
        return "rate_limited"
    if "http error 403" in lower:
        return "ip_blocked"
    return None


def _fetch_with_ytdlp(video_id: str, verbose: bool = False) -> TranscriptResult:
    """Fallback: use yt-dlp to download the auto-generated English subtitle track as VTT, parse it."""
    from yt_dlp import YoutubeDL

    ydl_opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "subtitleslangs": ["en", "en-US", "en-orig"],
        "subtitlesformat": "vtt",
    }
    cookies_file = _resolve_cookies_file()
    if cookies_file:
        ydl_opts["cookiefile"] = str(cookies_file)

    with tempfile.TemporaryDirectory() as tmp:
        out_template = os.path.join(tmp, "%(id)s.%(ext)s")
        ydl_opts["outtmpl"] = out_template
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_id, download=False)
                subs = info.get("subtitles") or {}
                auto = info.get("automatic_captions") or {}

                lang_pref = ["en-orig", "en", "en-US"]
                track = None
                for lang in lang_pref:
                    if lang in subs:
                        track = (subs[lang], "subtitle", lang)
                        break
                if track is None:
                    for lang in lang_pref:
                        if lang in auto:
                            track = (auto[lang], "auto", lang)
                            break

                if track is None:
                    return TranscriptResult(
                        video_id=video_id,
                        video_title="",
                        text="",
                        available=False,
                        error="yt-dlp fallback: no English subtitle track found",
                        reason="no_captions",
                    )

                formats, _kind, lang = track
                vtt_fmt = next((f for f in formats if f.get("ext") == "vtt"), formats[0])
                url = vtt_fmt["url"]
                if cookies_file:
                    ydl.params["cookiefile"] = str(cookies_file)
                ydl.download([url])
                vtt_path = Path(tmp) / f"{video_id}.{vtt_fmt['ext']}"
                if not vtt_path.exists():
                    candidates = list(Path(tmp).glob(f"{video_id}.*"))
                    if not candidates:
                        return TranscriptResult(
                            video_id=video_id,
                            video_title="",
                            text="",
                            available=False,
                            error="yt-dlp fallback: subtitle download produced no file",
                            reason="unknown",
                        )
                    vtt_path = candidates[0]

                text, segments = _parse_vtt(vtt_path.read_text(encoding="utf-8"))
                return TranscriptResult(
                    video_id=video_id,
                    video_title="",
                    text=text,
                    available=True,
                    segments=segments,
                    tier=f"yt-dlp/{lang}",
                )
        except Exception as e:
            reason = _classify_ytdlp_error(str(e)) or "unknown"
            return TranscriptResult(
                video_id=video_id,
                video_title="",
                text="",
                available=False,
                error=f"yt-dlp fallback failed: {type(e).__name__}: {str(e)[:200]}",
                reason=reason,
                cookies_needed=(reason == "ip_blocked"),
            )


def _parse_vtt(vtt: str) -> tuple[str, list[dict]]:
    """Parse a WebVTT subtitle file into plain text + timed segments."""
    text_parts: list[str] = []
    segments: list[dict] = []

    blocks = re.split(r"\n\n+", vtt.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines or lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        timing = None
        for ln in lines:
            if "-->" in ln:
                timing = ln
                break
        if not timing:
            continue
        m = re.match(
            r"(\d{2}:)?(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d{2}:)?(\d{2}):(\d{2})\.(\d{3})",
            timing,
        )
        if not m:
            continue
        start_h = int(m.group(1)[:-3] or 0) if m.group(1) else 0
        start = start_h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
        end_h = int(m.group(5)[:-3] or 0) if m.group(5) else 0
        end = end_h * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8)) / 1000.0

        text_lines = [
            re.sub(r"<[^>]+>", "", ln).strip()
            for ln in lines
            if "-->" not in ln and not re.match(r"^\d+$", ln.strip())
        ]
        text_lines = [t for t in text_lines if t]
        if not text_lines:
            continue
        joined = " ".join(text_lines)
        text_parts.append(joined)
        segments.append({"start": start, "duration": end - start, "text": joined})

    return " ".join(text_parts), segments


def _classify_youtube_api_error(exc: Exception) -> Optional[str]:
    """Return 'ip_blocked' / 'rate_limited' / 'no_captions' / 'unavailable' if we recognise it."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if name == "IpBlocked":
        return "ip_blocked"
    if "blocking requests from your ip" in msg or "blocking" in msg and "ip" in msg:
        return "ip_blocked"
    if name in ("TooManyRequests",) or "too many requests" in msg:
        return "rate_limited"
    if name in ("TranscriptsDisabled", "NoTranscriptFound", "NotTranslatable"):
        return "no_captions"
    if name in ("VideoUnavailable",):
        return "unavailable"
    if name in ("IpBlocked",):
        return "ip_blocked"
    if "429" in msg:
        return "rate_limited"
    return None


def _log_and_record_block(video_id: str, source: str, detail: str) -> None:
    """Log a clear [YOUTUBE-IP-BLOCKED] line and persist to DB so the UI can warn."""
    log.warning(
        "[YOUTUBE-IP-BLOCKED] video=%s source=%s detail=%s",
        video_id, source, detail[:200],
    )
    try:
        from src import db
        db.record_transcript_failure(video_id, "ip_blocked", f"{source}: {detail[:400]}")
        db.prune_transcript_failures(keep_days=14)
    except Exception as e:
        log.error("Failed to record transcript failure: %s", e)


def get_transcript(video_url: str, verbose: bool = False) -> TranscriptResult:
    video_id = get_video_id(video_url)

    if verbose:
        print(f"Fetching transcript for: {video_url} (ID: {video_id})")

    yt_api_err: Optional[Exception] = None
    yt_reason: Optional[str] = None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        transcript_data = None
        last_err: Optional[Exception] = None
        last_reason: Optional[str] = None
        for languages in (["en"], ["en-US"], ["en-orig"], None):
            try:
                if languages is None:
                    transcript_data = api.fetch(video_id)
                else:
                    transcript_data = api.fetch(video_id, languages=languages)
                break
            except Exception as e:
                last_err = e
                last_reason = _classify_youtube_api_error(e)
                continue

        if transcript_data is not None:
            text_parts = []
            segments = []
            for snippet in transcript_data:
                text = snippet.text.strip().replace("\n", " ")
                if text:
                    text_parts.append(text)
                    segments.append({
                        "start": float(snippet.start),
                        "duration": float(snippet.duration),
                        "text": text,
                    })
            return TranscriptResult(
                video_id=video_id,
                video_title="",
                text=" ".join(text_parts),
                available=True,
                segments=segments,
                tier="youtube-transcript-api",
            )

        yt_api_err = last_err
        yt_reason = last_reason
        if last_reason == "ip_blocked":
            _log_and_record_block(video_id, "youtube-transcript-api", str(last_err))
        if verbose:
            print(f"  youtube-transcript-api failed: {last_err}")
    except Exception as e:
        yt_api_err = e
        yt_reason = _classify_youtube_api_error(e)
        if yt_reason == "ip_blocked":
            _log_and_record_block(video_id, "youtube-transcript-api", str(e))
        if verbose:
            print(f"  youtube-transcript-api errored: {e}")

    if verbose:
        print("  Falling back to yt-dlp subtitle download...")
    fallback = _fetch_with_ytdlp(video_id, verbose=verbose)
    if fallback.available:
        return fallback

    if fallback.reason == "ip_blocked":
        _log_and_record_block(video_id, "yt-dlp-fallback", fallback.error or "")

    err = fallback.error or "yt-dlp fallback failed"
    yt_msg = ""
    if yt_api_err:
        yt_msg = f"; youtube-transcript-api: {type(yt_api_err).__name__}: {str(yt_api_err)[:120]}"

    reason = fallback.reason or yt_reason or "unknown"
    cookies_needed = (reason == "ip_blocked")

    return TranscriptResult(
        video_id=video_id,
        video_title="",
        text="",
        available=False,
        error=f"{err}{yt_msg}",
        reason=reason,
        cookies_needed=cookies_needed,
    )


def get_transcript_with_title(video_url: str, verbose: bool = False) -> TranscriptResult:
    metadata = get_video_metadata(video_url, verbose)
    transcript = get_transcript(video_url, verbose)
    transcript.video_title = metadata.title if metadata.available else 'Unknown'
    return transcript