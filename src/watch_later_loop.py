from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src import db
from src.config import DEFAULT_MODEL, get_output_dir
from src.youtube_watch_later import (
    WatchLaterEntry,
    fetch_all_watch_later,
)
from src import gmail_sender


log = logging.getLogger("ytscraper.watch_later")


_enabled = os.getenv("WATCH_LATER_ENABLED", "true").lower() in ("1", "true", "yes")
_interval_seconds = int(os.getenv("WATCH_LATER_POLL_INTERVAL", "3600"))
_startup_delay_seconds = int(os.getenv("WATCH_LATER_STARTUP_DELAY", "120"))


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


@dataclass
class SyncRunReport:
    started_at: str
    finished_at: Optional[str] = None
    discovered: int = 0
    processed: int = 0
    emailed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    processed_video_ids: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "discovered": self.discovered,
            "processed": self.processed,
            "emailed": self.emailed,
            "failed": self.failed,
            "errors": self.errors,
            "processed_video_ids": self.processed_video_ids,
            "error": self.error,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _discover_new() -> tuple[list[WatchLaterEntry], Optional[str]]:
    """Fetch WL from YouTube, persist new IDs, return only the new entries."""
    result = fetch_all_watch_later()
    if result.error:
        return [], result.error

    new_entries: list[WatchLaterEntry] = []
    for entry in result.entries:
        existing = db.get_watch_later_entry(entry.video_id)
        if existing is None:
            db.upsert_watch_later_entry(
                video_id=entry.video_id,
                video_title=entry.title,
                channel_name=entry.channel_title,
                video_url=entry.video_url,
                added_to_watch_later_at=entry.added_to_watch_later_at,
            )
            new_entries.append(entry)
    return new_entries, None


def _process_entry(entry: WatchLaterEntry, model: str):
    """Run the analysis pipeline. Returns (ok, error_message, summary_result)."""
    from src.cli import _process_single_video

    output_dir = get_output_dir()
    try:
        result = _process_single_video(
            video_url=entry.video_url,
            channel_name=entry.channel_title,
            model=model,
            output_path=output_dir,
            verbose=False,
            source="watch-later",
            channel_id=entry.channel_id or None,
        )
    except Exception as e:
        msg = f"pipeline exception: {e}"
        log.exception("Watch Later pipeline exception for %s", entry.video_id)
        return False, msg, None

    if result is None:
        return False, "no transcript available", None
    if not result.success:
        return False, result.error or "summarizer reported failure", result
    return True, None, result


def _send_email(summary) -> tuple[bool, Optional[str], Optional[str]]:
    try:
        sent = gmail_sender.send_analysis_email(summary)
        return True, None, sent.get("message_id")
    except Exception as e:
        return False, f"email send failed: {e}", None


def sync_once(model: str = DEFAULT_MODEL, limit: int = 25) -> SyncRunReport:
    """Single Watch Later pass. Returns a structured report."""
    report = SyncRunReport(started_at=_now_iso())

    try:
        new_entries, err = _discover_new()
    except Exception as e:
        report.error = f"discovery crashed: {e}"
        report.finished_at = _now_iso()
        log.exception("Watch Later discovery crashed")
        return report

    if err:
        report.error = err
        report.finished_at = _now_iso()
        log.error("Watch Later fetch error: %s", err)
        return report

    pending = db.list_unprocessed_watch_later_entries()
    report.discovered = len(new_entries)
    log.info(
        "Watch Later sync: %d new, %d pending (of %d total)",
        len(new_entries),
        len(pending),
        db.watch_later_stats()["total"],
    )

    for row in pending[:limit]:
        video_id = row["video_id"]
        video_url = row.get("video_url") or f"https://www.youtube.com/watch?v={video_id}"

        entry = WatchLaterEntry(
            video_id=video_id,
            title=row.get("video_title") or "",
            channel_title=row.get("channel_name") or "",
            channel_id="",
            video_url=video_url,
            added_to_watch_later_at=row.get("added_to_watch_later_at") or "",
        )

        ok, error, summary = _process_entry(entry, model)
        if not ok or summary is None:
            report.failed += 1
            report.errors.append(f"{video_id}: {error}")
            db.mark_watch_later_failed(video_id, error or "unknown")
            log.warning("Watch Later pipeline failed for %s: %s", video_id, error)
            continue

        db.mark_watch_later_processed(video_id)
        report.processed += 1

        emailed, email_error, message_id = _send_email(summary)
        if emailed:
            db.mark_watch_later_emailed(video_id, message_id)
            report.emailed += 1
            report.processed_video_ids.append(video_id)
            log.info(
                "Watch Later: emailed %s (%s) message_id=%s",
                video_id,
                entry.title[:50],
                message_id,
            )
        else:
            report.failed += 1
            report.errors.append(f"{video_id}: email: {email_error}")
            db.mark_watch_later_failed(video_id, email_error or "email failed")
            log.warning("Watch Later email failed for %s: %s", video_id, email_error)

    report.finished_at = _now_iso()
    log.info(
        "Watch Later sync done: processed=%d emailed=%d failed=%d",
        report.processed,
        report.emailed,
        report.failed,
    )
    return report


def _loop() -> None:
    if _startup_delay_seconds > 0:
        log.info("Watch Later loop waiting %ds before first run", _startup_delay_seconds)
        if _stop_event.wait(timeout=_startup_delay_seconds):
            return

    while not _stop_event.is_set():
        try:
            sync_once()
        except Exception as e:
            log.exception("Watch Later loop crashed: %s", e)
        log.info("Next Watch Later sync in %ds", _interval_seconds)
        if _stop_event.wait(timeout=_interval_seconds):
            return


def start_watch_later_loop() -> Optional[threading.Thread]:
    global _thread
    if not _enabled:
        log.info("Watch Later loop disabled via WATCH_LATER_ENABLED")
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="ytscraper-watch-later", daemon=True)
    _thread.start()
    log.info("Watch Later loop started (interval=%ds)", _interval_seconds)
    return _thread


def stop_watch_later_loop() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)


def get_status() -> dict:
    return {
        "enabled": _enabled,
        "interval_seconds": _interval_seconds,
        "thread_alive": bool(_thread and _thread.is_alive()),
        "stats": db.watch_later_stats(),
        "youtube_authenticated": _safe_youtube_auth(),
        "gmail_authenticated": _safe_gmail_auth(),
        "gmail_address": _safe_gmail_address(),
    }


def _safe_youtube_auth() -> bool:
    try:
        from src.youtube_watch_later import is_authenticated
        return is_authenticated()
    except Exception:
        return False


def _safe_gmail_auth() -> bool:
    try:
        return gmail_sender.is_authenticated()
    except Exception:
        return False


def _safe_gmail_address() -> Optional[str]:
    try:
        return gmail_sender.sender_address()
    except Exception:
        return None
