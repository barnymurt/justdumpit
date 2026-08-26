import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterator, Any

from src.config import get_data_dir, CONFIG_DIR


SCHEMA_PATH = Path(__file__).parent / "schema.sql"

CURRENT_PROMPT_VERSION = "v1"


_LEGACY_DB_PATH = CONFIG_DIR / "kb.db"
_LEGACY_OUTPUT_DIR = CONFIG_DIR / "output"
_LEGACY_CHANNELS_FILE = CONFIG_DIR / "channels.json"
_LEGACY_BACKUP_DIR = CONFIG_DIR / "backups"


def _is_empty_sqlite(path: Path) -> bool:
    import sqlite3
    try:
        with sqlite3.connect(str(path)) as c:
            n = c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            if n == 0:
                return True
            row_counts = []
            for (name,) in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall():
                row_counts.append(c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            return all(rc == 0 for rc in row_counts)
    except Exception:
        return False


def _migrate_legacy_files(data_dir: Path) -> None:
    for legacy, target_name in [
        (_LEGACY_DB_PATH, "kb.db"),
        (_LEGACY_CHANNELS_FILE, "channels.json"),
    ]:
        target = data_dir / target_name
        if not legacy.exists():
            continue
        if target.exists():
            if target_name == "kb.db" and not _is_empty_sqlite(target):
                continue
            if target_name == "channels.json":
                try:
                    if json.loads(target.read_text(encoding="utf-8")).get("channels"):
                        continue
                except Exception:
                    pass
            target.unlink()
        shutil.move(str(legacy), str(target))
    legacy_out = _LEGACY_OUTPUT_DIR
    if legacy_out.exists() and any(legacy_out.iterdir()):
        target_out = data_dir / "output"
        if not any(target_out.iterdir()):
            target_out.rmdir()
            shutil.move(str(legacy_out), str(target_out))
    legacy_bups = _LEGACY_BACKUP_DIR
    if legacy_bups.exists() and any(legacy_bups.iterdir()):
        target_bups = data_dir / "backups"
        if not any(target_bups.iterdir()):
            target_bups.rmdir()
            shutil.move(str(legacy_bups), str(target_bups))


def get_db_path() -> Path:
    import os
    env = os.getenv("YTSCRAPER_DB_PATH")
    if env:
        return Path(env)
    return get_data_dir() / "kb.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: Optional[Path] = None) -> Path:
    db_path = db_path or get_db_path()
    data_dir = get_data_dir()
    _migrate_legacy_files(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with connect(db_path) as conn:
        conn.executescript(schema_sql)
        conn.commit()

    return db_path


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    db_path = db_path or get_db_path()
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def upsert_video(
    video_id: str,
    url: str,
    title: Optional[str] = None,
    channel_id: Optional[str] = None,
    channel_name: Optional[str] = None,
    duration: Optional[int] = None,
    published_at: Optional[str] = None,
    transcript_tier: Optional[str] = None,
    transcript_available: bool = False,
) -> None:
    with connect() as conn:
        existing = conn.execute(
            "SELECT video_id FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, url, title, channel_id, channel_name,
                    duration, published_at, fetched_at,
                    transcript_tier, transcript_available
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id, url, title, channel_id, channel_name,
                    duration, published_at, now_iso(),
                    transcript_tier, 1 if transcript_available else 0,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE videos SET
                    url = COALESCE(?, url),
                    title = COALESCE(?, title),
                    channel_id = COALESCE(?, channel_id),
                    channel_name = COALESCE(?, channel_name),
                    duration = COALESCE(?, duration),
                    published_at = COALESCE(?, published_at),
                    transcript_tier = COALESCE(?, transcript_tier),
                    transcript_available = COALESCE(?, transcript_available)
                WHERE video_id = ?
                """,
                (
                    url, title, channel_id, channel_name,
                    duration, published_at, transcript_tier,
                    1 if transcript_available else 0,
                    video_id,
                ),
            )


def get_video(video_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return dict(row) if row else None


def list_videos(channel_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    with connect() as conn:
        if channel_id:
            rows = conn.execute(
                """
                SELECT * FROM videos
                WHERE channel_id = ?
                ORDER BY published_at DESC NULLS LAST
                LIMIT ?
                """,
                (channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM videos
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_transcript(
    video_id: str,
    raw_text: str,
    tier: str,
    segments: Optional[list[dict]] = None,
) -> None:
    segments_json = json.dumps(segments) if segments is not None else None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transcripts (video_id, raw_text, segments_json, tier, char_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                raw_text = excluded.raw_text,
                segments_json = excluded.segments_json,
                tier = excluded.tier,
                char_count = excluded.char_count,
                fetched_at = excluded.fetched_at
            """,
            (video_id, raw_text, segments_json, tier, len(raw_text), now_iso()),
        )
        conn.execute(
            "UPDATE videos SET transcript_available = 1, transcript_tier = ? WHERE video_id = ?",
            (tier, video_id),
        )


def get_transcript(video_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE video_id = ?", (video_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("segments_json"):
            try:
                result["segments"] = json.loads(result["segments_json"])
            except json.JSONDecodeError:
                result["segments"] = []
        return result


def upsert_analysis(
    video_id: str,
    output: dict,
    model: str,
    prompt_version: Optional[str] = None,
    markdown: Optional[str] = None,
    tldr: Optional[str] = None,
) -> None:
    prompt_version = prompt_version or CURRENT_PROMPT_VERSION
    output_json = json.dumps(output, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO analyses (
                video_id, prompt_version, model, output_json,
                markdown, tldr, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id, prompt_version) DO UPDATE SET
                model = excluded.model,
                output_json = excluded.output_json,
                markdown = excluded.markdown,
                tldr = excluded.tldr,
                created_at = excluded.created_at
            """,
            (video_id, prompt_version, model, output_json, markdown, tldr, now_iso()),
        )


def get_analysis(video_id: str, prompt_version: Optional[str] = None) -> Optional[dict]:
    prompt_version = prompt_version or CURRENT_PROMPT_VERSION
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE video_id = ? AND prompt_version = ?",
            (video_id, prompt_version),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["output"] = json.loads(result["output_json"])
        except json.JSONDecodeError:
            result["output"] = {}
        return result


def insert_chunks(
    video_id: str,
    chunks: list[dict],
    embedding_model: Optional[str] = None,
) -> None:
    """chunks: list of {chunk_index, text, start_ts?, end_ts?, char_start?, char_end?, embedding? (bytes)}"""
    import numpy as np

    with connect() as conn:
        conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
        for c in chunks:
            embedding = c.get("embedding")
            embedding_blob: Optional[bytes] = None
            if embedding is not None:
                if isinstance(embedding, np.ndarray):
                    embedding_blob = embedding.astype(np.float32).tobytes()
                elif isinstance(embedding, (bytes, bytearray)):
                    embedding_blob = bytes(embedding)
            conn.execute(
                """
                INSERT INTO chunks (
                    video_id, chunk_index, start_ts, end_ts,
                    text, char_start, char_end,
                    embedding, embedding_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    c["chunk_index"],
                    c.get("start_ts"),
                    c.get("end_ts"),
                    c["text"],
                    c.get("char_start"),
                    c.get("char_end"),
                    embedding_blob,
                    embedding_model,
                ),
            )


def list_chunks(video_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_index",
            (video_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def has_video(video_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone() is not None


def stats() -> dict:
    with connect() as conn:
        v = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
        t = conn.execute("SELECT COUNT(*) AS n FROM transcripts").fetchone()["n"]
        a = conn.execute("SELECT COUNT(*) AS n FROM analyses").fetchone()["n"]
        c = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        ch = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    return {"videos": v, "transcripts": t, "analyses": a, "chunks": c, "channels": ch}


def upsert_channel(
    channel_id: str,
    channel_url: str,
    channel_name: Optional[str] = None,
    last_seen_video_id: Optional[str] = None,
) -> None:
    with connect() as conn:
        existing = conn.execute(
            "SELECT channel_id FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO channels (
                    channel_id, channel_url, channel_name,
                    added_at, last_polled_at, last_seen_video_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel_id, channel_url, channel_name, now_iso(), None, last_seen_video_id),
            )
        else:
            conn.execute(
                """
                UPDATE channels SET
                    channel_url = ?,
                    channel_name = COALESCE(?, channel_name),
                    last_seen_video_id = COALESCE(?, last_seen_video_id)
                WHERE channel_id = ?
                """,
                (channel_url, channel_name, last_seen_video_id, channel_id),
            )


def mark_channel_polled(channel_id: str, last_seen_video_id: Optional[str] = None) -> None:
    with connect() as conn:
        if last_seen_video_id is not None:
            conn.execute(
                """
                UPDATE channels SET
                    last_polled_at = ?,
                    last_seen_video_id = COALESCE(?, last_seen_video_id)
                WHERE channel_id = ?
                """,
                (now_iso(), last_seen_video_id, channel_id),
            )
        else:
            conn.execute(
                "UPDATE channels SET last_polled_at = ? WHERE channel_id = ?",
                (now_iso(), channel_id),
            )


def list_channels() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM channels ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def remove_channel(channel_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        return cur.rowcount > 0


def record_transcript_failure(video_id: str, reason: str, detail: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO transcript_failures (video_id, reason, detail, happened_at)
            VALUES (?, ?, ?, ?)
            """,
            (video_id, reason, (detail or "")[:500], now_iso()),
        )


def get_recent_failures(reason: Optional[str] = None, since_hours: int = 24) -> list[dict]:
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        if reason:
            rows = conn.execute(
                """
                SELECT * FROM transcript_failures
                WHERE happened_at >= ? AND reason = ?
                ORDER BY happened_at DESC
                """,
                (cutoff, reason),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM transcript_failures
                WHERE happened_at >= ?
                ORDER BY happened_at DESC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


def prune_transcript_failures(keep_days: int = 14) -> int:
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        cur = conn.execute("DELETE FROM transcript_failures WHERE happened_at < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Watch Later pipeline
# ---------------------------------------------------------------------------


def upsert_watch_later_entry(
    video_id: str,
    video_title: Optional[str] = None,
    channel_name: Optional[str] = None,
    video_url: Optional[str] = None,
    added_to_watch_later_at: Optional[str] = None,
) -> None:
    """Record a video that we just discovered in the user's Watch Later."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT video_id, attempts FROM watch_later_processed WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO watch_later_processed (
                    video_id, video_title, channel_name, video_url,
                    added_to_watch_later_at, discovered_at, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    video_id, video_title, channel_name, video_url,
                    added_to_watch_later_at, now_iso(),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE watch_later_processed SET
                    video_title = COALESCE(?, video_title),
                    channel_name = COALESCE(?, channel_name),
                    video_url = COALESCE(?, video_url),
                    added_to_watch_later_at = COALESCE(?, added_to_watch_later_at)
                WHERE video_id = ?
                """,
                (video_title, channel_name, video_url, added_to_watch_later_at, video_id),
            )


def mark_watch_later_processed(video_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE watch_later_processed
            SET processed_at = ?,
                attempts = attempts + 1,
                last_error = NULL
            WHERE video_id = ?
            """,
            (now_iso(), video_id),
        )


def mark_watch_later_emailed(video_id: str, email_message_id: Optional[str]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE watch_later_processed
            SET emailed_at = ?, email_message_id = ?
            WHERE video_id = ?
            """,
            (now_iso(), email_message_id, video_id),
        )


def mark_watch_later_failed(video_id: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE watch_later_processed
            SET last_error = ?,
                attempts = attempts + 1
            WHERE video_id = ?
            """,
            ((error or "")[:500], video_id),
        )


def get_watch_later_entry(video_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM watch_later_processed WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        return dict(row) if row else None


def list_unprocessed_watch_later_entries() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watch_later_processed
            WHERE processed_at IS NULL
            ORDER BY discovered_at
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_watch_later_entries(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM watch_later_processed
            ORDER BY discovered_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def watch_later_stats() -> dict:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM watch_later_processed").fetchone()["n"]
        processed = conn.execute(
            "SELECT COUNT(*) AS n FROM watch_later_processed WHERE processed_at IS NOT NULL"
        ).fetchone()["n"]
        emailed = conn.execute(
            "SELECT COUNT(*) AS n FROM watch_later_processed WHERE emailed_at IS NOT NULL"
        ).fetchone()["n"]
        last_processed = conn.execute(
            "SELECT MAX(processed_at) AS t FROM watch_later_processed"
        ).fetchone()["t"]
    return {
        "total": total,
        "processed": processed,
        "emailed": emailed,
        "pending": total - processed,
        "last_processed_at": last_processed,
    }