from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src import db
from src.feeds import new_videos_since, fetch_channel_feed
from src.registry import list_channels, add_channel


log = logging.getLogger(__name__)


def sync_channel(channel_id: str, limit: int = 15, dry_run: bool = False, verbose: bool = False) -> dict:
    channels = {c["channel_id"]: c for c in list_channels()}
    record = channels.get(channel_id, {})
    last_seen = record.get("last_seen_video_id")

    new_entries, latest = new_videos_since(channel_id, last_seen, verbose=verbose)

    if not new_entries:
        db.mark_channel_polled(channel_id, latest)
        return {"channel_id": channel_id, "new_count": 0, "errors": 0, "new_video_ids": []}

    new_entries = new_entries[:limit]
    new_video_ids: list[str] = []
    errors = 0

    if dry_run:
        for entry in new_entries:
            new_video_ids.append(entry.video_id)
        return {
            "channel_id": channel_id,
            "new_count": len(new_entries),
            "errors": 0,
            "new_video_ids": new_video_ids,
            "dry_run": True,
        }

    from src.cli import _process_single_video
    from src.config import get_output_dir, DEFAULT_MODEL

    output_path = get_output_dir()

    for entry in new_entries:
        if verbose:
            log.info(f"Processing: {entry.title}")
        try:
            result = _process_single_video(
                video_url=entry.url,
                channel_name=entry.channel_name or record.get("channel_name", "Unknown"),
                model=DEFAULT_MODEL,
                output_path=output_path,
                verbose=False,
                source="rss-sync",
                channel_id=channel_id,
            )
            if result is not None:
                new_video_ids.append(entry.video_id)
            else:
                errors += 1
        except Exception as e:
            errors += 1
            log.error(f"Failed to process {entry.video_id}: {e}")

    db.mark_channel_polled(channel_id, latest)

    return {
        "channel_id": channel_id,
        "new_count": len(new_video_ids),
        "errors": errors,
        "new_video_ids": new_video_ids,
    }


def sync_all(limit: int = 15, dry_run: bool = False, verbose: bool = False) -> list[dict]:
    results = []
    for record in list_channels():
        if verbose:
            log.info(f"Syncing: {record['channel_name']} ({record['channel_id']})")
        result = sync_channel(record["channel_id"], limit=limit, dry_run=dry_run, verbose=verbose)
        result["channel_name"] = record.get("channel_name")
        results.append(result)
    return results


def backfill_channel(channel_id: str, limit: int = 5, verbose: bool = False) -> list[dict]:
    result = fetch_channel_feed(channel_id, verbose=verbose)
    if result.error or not result.entries:
        return []

    entries = result.entries[:limit]
    results = []

    from src.cli import _process_single_video
    from src.config import get_output_dir, DEFAULT_MODEL

    output_path = get_output_dir()

    for entry in entries:
        try:
            summary = _process_single_video(
                video_url=entry.url,
                channel_name=entry.channel_name or 'Unknown',
                model=DEFAULT_MODEL,
                output_path=output_path,
                verbose=False,
                source="backfill",
                channel_id=channel_id,
            )
            results.append({
                "video_id": entry.video_id,
                "title": entry.title,
                "success": summary is not None and summary.success,
            })
        except Exception as e:
            results.append({
                "video_id": entry.video_id,
                "title": entry.title,
                "success": False,
                "error": str(e),
            })

    return results


def start_sync_run(channel_id: str) -> int:
    started_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO sync_log (channel_id, started_at, new_videos, errors) VALUES (?, ?, 0, 0)",
            (channel_id, started_at),
        )
        return cur.lastrowid


def finish_sync_run(run_id: int, new_videos: int, errors: int, notes: Optional[str] = None) -> None:
    finished_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE sync_log
            SET finished_at = ?, new_videos = ?, errors = ?, notes = ?
            WHERE id = ?
            """,
            (finished_at, new_videos, errors, notes, run_id),
        )