from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src import db
from src.config import get_api_key, DEFAULT_MODEL, get_output_dir
from src.registry import list_channels as registry_list_channels
from src.search import semantic_search
from src.scheduler_loop import start_scheduler_loop


STATIC_DIR = Path(__file__).parent / "static"


app = FastAPI(
    title="YTTranscriptScraper API",
    version="1.0.0",
    description="Knowledge base for YouTube video transcripts.",
)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    start_scheduler_loop()
    try:
        from src.watch_later_loop import start_watch_later_loop
        start_watch_later_loop()
    except Exception as e:
        logging.getLogger("ytscraper").exception("Watch Later loop failed to start: %s", e)


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"name": "YTTranscriptScraper API", "docs": "/docs", "endpoints": [
        "POST /analyse", "GET /video/{id}", "POST /search", "GET /channels", "GET /stats"
    ]}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
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
    }


class AnalyseRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL or 11-char video ID")
    model: Optional[str] = Field(None)
    prompt_version: str = Field("v2", description="v1 (legacy) or v2 (atoms + Stage 2)")
    send_email: bool = Field(False, description="Email the analysis after Stage 2")


def _do_analyse(url: str, model: str) -> dict:
    from src.cli import _process_single_video

    output_path = get_output_dir()
    output_path.mkdir(parents=True, exist_ok=True)
    if not url.startswith('http'):
        url = f"https://www.youtube.com/watch?v={url}"

    result = _process_single_video(
        video_url=url,
        channel_name="",
        model=model,
        output_path=output_path,
        verbose=False,
        source="api",
    )
    if result is None:
        raise HTTPException(status_code=400, detail="No transcript available")
    if not result.success:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {result.error}")
    return {
        "video_id": result.video_id,
        "video_title": result.video_title,
        "video_url": result.video_url,
        "channel_name": result.channel_name,
        "tldr": result.tldr,
        "argument": result.argument,
        "key_concepts": result.key_concepts,
        "takeaways": result.takeaways,
        "claims_to_verify": result.claims_to_verify,
        "glossary": result.glossary,
        "markdown": result.markdown,
        "prompt_version": result.prompt_version,
        "model": model,
    }


@app.post("/analyse")
def analyse(req: AnalyseRequest, background: BackgroundTasks):
    try:
        get_api_key()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    model = req.model or DEFAULT_MODEL

    try:
        result = _do_analyse(req.url, model)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")

    payload = result.copy() if hasattr(result, "copy") else dict(result)

    if req.prompt_version == "v2" and payload.get("transferable_atoms"):
        try:
            from src.stage2 import score_video, persist_stage2, load_goals
            cfg = load_goals()
            s2 = score_video(
                video_id=payload["video_id"],
                extraction=payload,
                cfg=cfg,
                model=model,
            )
            persist_stage2(payload["video_id"], s2, prompt_version="v2")
            payload["stage2"] = s2.to_dict()
        except Exception as e:
            payload["stage2_error"] = str(e)

    return payload


@app.get("/video/{video_id}")
def get_video(video_id: str):
    video = db.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    analysis = db.get_analysis(video_id)
    transcript = db.get_transcript(video_id)
    chunks = db.list_chunks(video_id)
    return {
        "video": video,
        "analysis": analysis,
        "has_transcript": transcript is not None,
        "transcript_length": transcript["char_count"] if transcript else 0,
        "chunk_count": len(chunks),
    }


@app.get("/video/{video_id}/transcript")
def get_video_transcript(video_id: str):
    transcript = db.get_transcript(video_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="No transcript stored")
    return {
        "video_id": video_id,
        "tier": transcript.get("tier"),
        "char_count": transcript.get("char_count"),
        "text": transcript.get("raw_text", ""),
        "segments": transcript.get("segments", []),
    }


@app.get("/video/{video_id}/transcript/range")
def get_transcript_range_endpoint(
    video_id: str,
    start: float = 0.0,
    end: float = 99999.0,
):
    """Return transcript segments within [start, end] seconds.

    Used by the downstream action-agent to fetch just the timestamp range
    referenced by an atom, instead of loading the full transcript.
    """
    if start < 0 or end < start:
        raise HTTPException(status_code=400, detail="start must be >= 0 and <= end")
    result = db.get_transcript_range(video_id, start, end)
    if result is None:
        raise HTTPException(status_code=404, detail="No transcript stored for this video")
    return result


@app.post("/watch-later/score")
def watch_later_score_endpoint(
    video_id: str,
    prompt_version: str = "v2",
    send_email_flag: bool = False,
):
    """Re-run Stage 2 against a stored v2 extraction using current goals.yaml."""
    from src.stage2 import score_video, persist_stage2, load_goals

    extraction_row = db.get_analysis(video_id, prompt_version=prompt_version)
    if not extraction_row:
        raise HTTPException(
            status_code=404,
            detail=f"No {prompt_version} analysis stored for {video_id}",
        )
    extraction = extraction_row.get("output", {})
    if not extraction.get("transferable_atoms"):
        raise HTTPException(
            status_code=400,
            detail="Stored extraction has no transferable_atoms",
        )

    cfg = load_goals()
    s2 = score_video(video_id=video_id, extraction=extraction, cfg=cfg)
    persist_stage2(video_id, s2, prompt_version=prompt_version)
    payload = s2.to_dict()

    if send_email_flag:
        from src.summarizer import SummaryResult
        result = SummaryResult(
            video_id=video_id,
            video_title=extraction.get("meta", {}).get("title", ""),
            video_url=extraction.get("meta", {}).get("url", ""),
            channel_name=extraction.get("meta", {}).get("channel", ""),
            summary=extraction.get("tldr", ""),
            key_points=[],
            important_links=[],
            timestamp_topics=[],
            transcript_length=0,
            chunks_used=0,
            success=True,
            prompt_version=prompt_version,
            tldr=extraction.get("tldr", ""),
            argument=extraction.get("argument", ""),
            markdown=extraction.get("markdown", ""),
            structured_output=extraction,
            atoms=extraction.get("transferable_atoms", []) or [],
            stack=extraction.get("stack", []) or [],
            open_questions=extraction.get("open_questions", []) or [],
            thesis=extraction.get("thesis", ""),
        )
        try:
            get_api_key()
        except ValueError as e:
            raise HTTPException(status_code=503, detail=str(e))
        from src import gmail_sender
        sent = gmail_sender.send_analysis_email(result, stage2=payload)
        payload["email_message_id"] = sent["message_id"]

    return payload


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)
    video_id: Optional[str] = None


@app.post("/search")
def search_endpoint(req: SearchRequest):
    try:
        results = semantic_search(req.query, k=req.k, video_id=req.video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")
    return {
        "query": req.query,
        "k": req.k,
        "results": results,
    }


@app.get("/channels")
def channels_endpoint():
    return {"channels": registry_list_channels()}


class AddChannelRequest(BaseModel):
    url: str = Field(..., min_length=1)


@app.post("/channels")
def add_channel_endpoint(req: AddChannelRequest):
    from src.registry import add_channel
    try:
        result = add_channel(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.delete("/channels/{channel_id}")
def remove_channel_endpoint(channel_id: str):
    from src.registry import remove_channel
    removed = remove_channel(channel_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"removed": channel_id}


@app.get("/stats")
def stats_endpoint():
    return db.stats()


@app.get("/videos")
def list_videos_endpoint(limit: int = 50):
    return {"videos": db.list_videos(limit=limit)}


@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# Watch Later pipeline endpoints
# ---------------------------------------------------------------------------


@app.get("/watch-later/status")
def watch_later_status_endpoint():
    from src.watch_later_loop import get_status
    try:
        return get_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"status failed: {e}")


@app.post("/watch-later/sync")
def watch_later_sync_endpoint(
    limit: int = 25,
    model: Optional[str] = None,
):
    try:
        get_api_key()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    from src.watch_later_loop import sync_once
    from src.config import DEFAULT_MODEL

    report = sync_once(model=model or DEFAULT_MODEL, limit=limit)
    if report.error:
        raise HTTPException(status_code=500, detail=report.error)
    return report.to_dict()


@app.get("/watch-later/entries")
def watch_later_entries_endpoint(
    limit: int = 50,
    only_pending: bool = False,
):
    rows = (
        db.list_unprocessed_watch_later_entries()
        if only_pending
        else db.list_watch_later_entries(limit=limit)
    )
    return {"entries": rows}