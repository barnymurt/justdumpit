from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

from src.scheduler import sync_all


log = logging.getLogger("ytscraper.scheduler_loop")


_enabled = os.getenv("YTSCRAPER_DISABLE_SCHEDULER", "").lower() not in ("1", "true", "yes")
_interval_seconds = int(os.getenv("YTSCRAPER_SYNC_INTERVAL", str(6 * 3600)))
_startup_delay_seconds = int(os.getenv("YTSCRAPER_SYNC_STARTUP_DELAY", "60"))


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _run_once() -> None:
    try:
        log.info("RSS sync starting...")
        results = sync_all(limit=15, dry_run=False, verbose=False)
        total_new = sum(r.get("new_count", 0) for r in results)
        total_err = sum(r.get("errors", 0) for r in results)
        log.info(f"RSS sync done: {total_new} new videos, {total_err} errors")
    except Exception as e:
        log.exception(f"RSS sync crashed: {e}")


def _loop() -> None:
    if _startup_delay_seconds > 0:
        log.info(f"Scheduler loop waiting {_startup_delay_seconds}s before first run")
        if _stop_event.wait(timeout=_startup_delay_seconds):
            return

    while not _stop_event.is_set():
        _run_once()
        log.info(f"Next sync in {_interval_seconds}s")
        if _stop_event.wait(timeout=_interval_seconds):
            return


def start_scheduler_loop() -> Optional[threading.Thread]:
    global _thread
    if not _enabled:
        log.info("Scheduler loop disabled via YTSCRAPER_DISABLE_SCHEDULER")
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="ytscraper-scheduler", daemon=True)
    _thread.start()
    log.info(f"Scheduler loop started (interval={_interval_seconds}s)")
    return _thread


def stop_scheduler_loop() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)