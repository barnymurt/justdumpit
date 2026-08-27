import sys
import typer
import os
import json
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        pass

from src.config import get_output_dir, get_api_key, DEFAULT_MODEL, DEFAULT_PROMPT_VERSION, MAX_VIDEO_SELECT, get_backup_dir
from src import db
from src.channel import get_channel_videos, print_video_list
from src.transcript import (
    get_transcript_with_title,
    get_video_metadata,
    get_video_id,
)
from src.summarizer import summarize_transcript, SummaryResult
from src.output import save_summary, print_summary_result
from src.embeddings import embed_texts, get_embedder

app = typer.Typer(help="YTTranscriptScraper - YouTube knowledge harvester")


def _ensure_db() -> None:
    db.init_db()


def _summary_to_db_payload(result: SummaryResult) -> dict:
    payload = {
        "summary": result.summary,
        "tldr": result.tldr,
        "argument": result.argument,
        "key_concepts": result.key_concepts,
        "takeaways": result.takeaways,
        "claims_to_verify": result.claims_to_verify,
        "glossary": result.glossary,
        "key_points": result.key_points,
        "transcript_length": result.transcript_length,
        "chunks_used": result.chunks_used,
        "success": result.success,
        "error": result.error,
        "chunk_extractions": result.chunk_extractions,
    }
    if (result.prompt_version or "v1") == "v2":
        payload["transferable_atoms"] = result.atoms
        payload["stack"] = result.stack
        payload["open_questions"] = result.open_questions
        payload["thesis"] = result.thesis
        meta = result.structured_output.get("meta") if result.structured_output else None
        if meta:
            payload["meta"] = meta
        for atom in result.atoms:
            ts = atom.get("timestamp_seconds") or atom.get("timestamp")
            if ts and "timestamp_seconds" not in atom:
                atom["timestamp_seconds"] = ts
    return payload


def _store_embeddings(video_id: str, chunks: list[dict], verbose: bool = False) -> None:
    if not chunks:
        return
    try:
        embedder = get_embedder()
        texts = [c['text'] for c in chunks]
        if verbose:
            typer.echo(f"  Embedding {len(texts)} chunks with {embedder.model_name}...")
        vectors = embedder.encode(texts)
        for i, c in enumerate(chunks):
            c['embedding'] = vectors[i]
        db.insert_chunks(video_id, chunks, embedding_model=embedder.model_name)
    except Exception as e:
        typer.echo(f"  [WARN] Embedding generation failed: {e}")


def _process_single_video(
    video_url: str,
    channel_name: str,
    model: str,
    output_path: Path,
    verbose: bool,
    source: str,
    channel_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> Optional[SummaryResult]:
    metadata = get_video_metadata(video_url, verbose=verbose)
    if not metadata.available:
        typer.echo(f"  [WARN] Could not fetch metadata: {metadata.error}")

    db.upsert_video(
        video_id=metadata.video_id,
        url=video_url if video_url.startswith('http') else metadata.url,
        title=metadata.title if metadata.available else None,
        channel_id=channel_id or metadata.channel_id,
        channel_name=channel_name or metadata.channel_name,
        duration=metadata.duration,
        published_at=metadata.published_at,
    )

    transcript_result = get_transcript_with_title(video_url, verbose=verbose)
    if not transcript_result.available:
        typer.echo(f"  [WARN] No transcript available: {transcript_result.error}")
        return None

    db.upsert_transcript(
        video_id=transcript_result.video_id,
        raw_text=transcript_result.text,
        tier="youtube-transcript-api",
        segments=transcript_result.segments,
    )

    summary_result = summarize_transcript(
        transcript_text=transcript_result.text,
        video_title=transcript_result.video_title or metadata.title,
        video_url=video_url,
        channel_name=channel_name or metadata.channel_name or "Unknown",
        video_id=transcript_result.video_id,
        model=model,
        verbose=verbose,
        prompt_version=prompt_version,
        segments=transcript_result.segments,
    )

    chunk_records = []
    if summary_result.chunk_extractions:
        for ce in summary_result.chunk_extractions:
            chunk_records.append({
                'chunk_index': ce['chunk_index'],
                'text': '',
                'char_start': None,
                'char_end': None,
            })

    if summary_result.success:
        json_path, md_path = save_summary(summary_result, output_path)
        typer.echo(f"  [OK] Saved: {json_path.name}")
        typer.echo(f"  [OK] Saved: {md_path.name}")

        from src.chunker import chunk_transcript_preserve_context
        chunks = chunk_transcript_preserve_context(transcript_result.text)
        chunk_records = []
        offset = 0
        for c in chunks:
            text = c['text']
            chunk_records.append({
                'chunk_index': c['chunk_index'],
                'text': text,
                'char_start': offset,
                'char_end': offset + len(text),
            })
            offset += len(text) + 2

        _store_embeddings(summary_result.video_id, chunk_records, verbose=verbose)

        output_payload = _summary_to_db_payload(summary_result)
        db.upsert_analysis(
            video_id=summary_result.video_id,
            output=output_payload,
            model=model,
            prompt_version=summary_result.prompt_version,
            markdown=summary_result.markdown or (md_path.read_text(encoding='utf-8') if md_path.exists() else None),
            tldr=summary_result.tldr or summary_result.summary,
        )
    else:
        typer.echo(f"  [WARN] Summary failed: {summary_result.error}")

    return summary_result


@app.command()
def analyse(
    video_url: str = typer.Argument(..., help="YouTube video URL or video ID"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m"),
    prompt_version: str = typer.Option(
        DEFAULT_PROMPT_VERSION,
        "--prompt-version",
        "-p",
        help="Prompt version: v1 (legacy editorial) or v2 (atoms + Stage 2)",
    ),
    no_stage2: bool = typer.Option(False, "--no-stage2", help="Skip Stage 2 (atoms only)"),
    send_email_flag: bool = typer.Option(
        False, "--send-email", help="Email the analysis after Stage 2"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run the full pipeline on a single YouTube URL and persist to the knowledge base.

    Default: v2 prompt + Stage 2 goal-conditioned scoring. Use --prompt-version v1
    for the legacy editorial summary, or --no-stage2 to skip the action brief.
    """
    _ensure_db()

    try:
        get_api_key()
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    output_path = output_dir or get_output_dir()
    output_path.mkdir(parents=True, exist_ok=True)

    if not video_url.startswith('http'):
        video_id = get_video_id(video_url)
        video_url = f"https://www.youtube.com/watch?v={video_id}"

    typer.echo(f"Analyse: {video_url}  (prompt={prompt_version})")

    result = _process_single_video(
        video_url=video_url,
        channel_name="",
        model=model,
        output_path=output_path,
        verbose=verbose,
        source="manual",
        prompt_version=prompt_version,
    )

    if result is None:
        raise typer.Exit(1)
    if not result.success:
        raise typer.Exit(2)

    stage2_payload = None
    if not no_stage2 and prompt_version == "v2":
        from src.stage2 import score_video, persist_stage2, load_goals
        try:
            cfg = load_goals()
            s2 = score_video(
                video_id=result.video_id,
                extraction=result.structured_output,
                cfg=cfg,
                model=model,
                verbose=verbose,
            )
            stage2_payload = s2.to_dict()
            persist_stage2(result.video_id, s2, prompt_version=prompt_version)
            typer.echo(f"  Stage 2: max relevance {max((g.get('relevance', 0) for g in (stage2_payload.get('per_goal') or [])), default=0)}/3")
        except Exception as e:
            typer.echo(f"  [WARN] Stage 2 failed (continuing): {e}", err=True)

    if send_email_flag:
        from src import gmail_sender
        try:
            sent = gmail_sender.send_analysis_email(
                result,
                stage2=stage2_payload,
            )
            typer.echo(f"  [EMAIL] {sent['message_id']}")
        except Exception as e:
            typer.echo(f"  [EMAIL-FAIL] {e}", err=True)

    print_summary_result(result)


@app.command()
def transcript(
    channel_url: str = typer.Argument(..., help="YouTube channel URL or video URL"),
    videos: Optional[str] = typer.Option(None, "--videos", "-v"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Channel-based batch flow: list videos, pick, transcribe. Persists to KB."""
    _ensure_db()

    try:
        get_api_key()
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    output_path = output_dir or get_output_dir()
    output_path.mkdir(parents=True, exist_ok=True)

    if verbose:
        typer.echo(f"Channel URL: {channel_url}")
        typer.echo(f"Output directory: {output_path}")

    channel = get_channel_videos(channel_url, verbose)

    if dry_run:
        print_video_list(channel)
        raise typer.Exit(0)

    selected_indices = _parse_video_selection(videos, len(channel.videos))

    if not selected_indices:
        print_video_list(channel)
        selected_indices = _prompt_video_selection(len(channel.videos))

    selected_videos = [channel.videos[i - 1] for i in selected_indices]

    typer.echo(f"\nSelected {len(selected_videos)} video(s) for transcription...\n")

    results = []
    for i, video in enumerate(selected_videos, 1):
        typer.echo(f"[{i}/{len(selected_videos)}] Processing: {video.title[:50]}...")

        result = _process_single_video(
            video_url=video.url,
            channel_name=channel.name,
            model=model,
            output_path=output_path,
            verbose=verbose,
            source="channel-batch",
        )

        if result is not None:
            results.append(result)

        if i < len(selected_videos):
            typer.echo("")

    _print_summary(results, output_path)


@app.command(name="db")
def db_stats():
    """Show knowledge base statistics."""
    _ensure_db()
    s = db.stats()
    typer.echo("\nKnowledge base:")
    typer.echo(f"  Videos:     {s['videos']}")
    typer.echo(f"  Transcripts:{s['transcripts']}")
    typer.echo(f"  Analyses:   {s['analyses']}")
    typer.echo(f"  Chunks:     {s['chunks']}")
    typer.echo(f"  Channels:   {s['channels']}")
    typer.echo(f"\nDB path: {db.get_db_path()}")


@app.command(name="videos")
def videos_list(limit: int = typer.Option(20, "--limit", "-n")):
    """List videos in the knowledge base, newest first."""
    _ensure_db()
    rows = db.list_videos(limit=limit)
    if not rows:
        typer.echo("No videos yet.")
        return
    typer.echo(f"\n{len(rows)} video(s):\n")
    for r in rows:
        title = (r.get('title') or '(no title)')[:60]
        ch = r.get('channel_name') or '(unknown channel)'
        typer.echo(f"  {r['video_id']}  {title}")
        typer.echo(f"    channel: {ch}  tier: {r.get('transcript_tier') or '-'}")


@app.command(name="add-channel")
def add_channel_cmd(
    url: str = typer.Argument(..., help="YouTube channel URL or @handle"),
):
    """Add a YouTube channel to the watch list."""
    from src.registry import add_channel as registry_add
    _ensure_db()
    try:
        get_api_key()
    except ValueError:
        pass
    try:
        result = registry_add(url)
        typer.echo(f"Added: {result['channel_name']} ({result['channel_id']})")
        typer.echo(f"  URL: {result['channel_url']}")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command(name="remove-channel")
def remove_channel_cmd(
    channel: str = typer.Argument(..., help="Channel ID, @handle, or URL"),
):
    """Remove a channel from the watch list."""
    from src.registry import remove_channel as registry_remove
    _ensure_db()
    if registry_remove(channel):
        typer.echo(f"Removed: {channel}")
    else:
        typer.echo(f"Not found: {channel}")


@app.command(name="channels")
def channels_cmd():
    """List watched channels with last-poll status."""
    _ensure_db()
    from src.registry import list_channels
    rows = list_channels()
    if not rows:
        typer.echo("No channels watched. Use: python -m src.cli add-channel <url>")
        return
    typer.echo(f"\n{len(rows)} watched channel(s):\n")
    for r in rows:
        name = r.get('channel_name') or '(unknown)'
        last_poll = r.get('last_polled_at') or 'never'
        last_seen = r.get('last_seen_video_id') or '-'
        typer.echo(f"  {name}  ({r['channel_id']})")
        typer.echo(f"    last polled: {last_poll}  last seen: {last_seen}")


@app.command()
def sync(
    channel: Optional[str] = typer.Option(None, "--channel", "-c", help="Sync only this channel ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be ingested without processing"),
    limit: int = typer.Option(15, "--limit", "-n", help="Max new videos per channel"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Poll RSS feeds for new videos and ingest them."""
    _ensure_db()
    try:
        get_api_key()
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    from src.scheduler import sync_channel, sync_all

    if channel:
        results = [sync_channel(channel, limit=limit, dry_run=dry_run, verbose=verbose)]
    else:
        results = sync_all(limit=limit, dry_run=dry_run, verbose=verbose)

    typer.echo("\n" + "=" * 60)
    typer.echo("Sync results")
    typer.echo("=" * 60)
    total_new = 0
    total_errors = 0
    for r in results:
        name = r.get('channel_name') or r['channel_id']
        typer.echo(f"  {name}: {r['new_count']} new, {r['errors']} errors")
        total_new += r['new_count']
        total_errors += r['errors']
    typer.echo("=" * 60)
    typer.echo(f"  Total: {total_new} new, {total_errors} errors")


@app.command()
def backfill(
    url: str = typer.Argument(..., help="YouTube channel URL or @handle"),
    limit: int = typer.Option(5, "--limit", "-n"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Catch-up ingest the N most recent videos from a channel."""
    _ensure_db()
    try:
        get_api_key()
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    from src.feeds import resolve_channel_id
    from src.scheduler import backfill_channel
    from src.registry import add_channel

    cid, canonical_url, name = resolve_channel_id(url)
    if not cid:
        typer.echo(f"Could not resolve channel from: {url}", err=True)
        raise typer.Exit(1)

    add_channel(url, channel_id=cid, channel_name=name)
    typer.echo(f"Backfilling {limit} videos from: {name}")

    results = backfill_channel(cid, limit=limit, verbose=verbose)
    for r in results:
        marker = "OK" if r.get('success') else "FAIL"
        typer.echo(f"  [{marker}] {r.get('video_id')}: {(r.get('title') or '')[:60]}")

    successful = sum(1 for r in results if r.get('success'))
    typer.echo(f"\n{successful}/{len(results)} successful")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search the knowledge base"),
    k: int = typer.Option(10, "--limit", "-n"),
    video_id: Optional[str] = typer.Option(None, "--video", help="Restrict to one video"),
):
    """Semantic search across chunked video transcripts."""
    _ensure_db()
    from src.search import semantic_search
    results = semantic_search(query, k=k, video_id=video_id)
    if not results:
        typer.echo("No matches.")
        return
    typer.echo(f"\nTop {len(results)} matches for: {query}\n")
    for r in results:
        typer.echo(f"  [{r['score']:.3f}] {r.get('video_id')} chunk {r.get('chunk_index')}")
        typer.echo(f"    {(r.get('text') or '')[:150]}...")


@app.command()
def server(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8080, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Run the FastAPI server (for local dev or behind a reverse proxy)."""
    _ensure_db()
    import uvicorn
    uvicorn.run(
        "src.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def backup(
    keep: int = typer.Option(14, "--keep", "-k", help="Number of backups to retain"),
    dest: Optional[Path] = typer.Option(None, "--dest", help="Backup destination"),
):
    """Snapshot the knowledge base to a timestamped file."""
    from datetime import datetime, timezone
    import shutil

    src_db = db.get_db_path()
    if not src_db.exists():
        typer.echo(f"No DB at {src_db}", err=True)
        raise typer.Exit(1)

    backup_dir = dest or get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_path = backup_dir / f"kb-{ts}.db"
    shutil.copy2(src_db, backup_path)

    backups = sorted(backup_dir.glob("kb-*.db"), key=lambda p: p.name)
    if len(backups) > keep:
        for old in backups[:len(backups) - keep]:
            old.unlink()

    typer.echo(f"Saved: {backup_path}")
    typer.echo(f"Retained: {min(len(backups), keep)} backup(s)")


# ---------------------------------------------------------------------------
# Watch Later pipeline commands
# ---------------------------------------------------------------------------


@app.command(name="watch-later-auth")
def watch_later_auth_cmd(
    headless: bool = typer.Option(
        False,
        "--headless",
        help="Print auth URL and read code manually (for SSH/CI/no-browser envs)",
    ),
):
    """Run OAuth2 for the YouTube Watch Later playlist (scope: youtube.readonly)."""
    from src import youtube_watch_later
    try:
        path = youtube_watch_later.run_local_auth(headless=headless)
    except Exception as e:
        typer.echo(f"Auth failed: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Saved YouTube token: {path}")
    typer.echo("Verify with: python -m src.cli watch-later-status")


@app.command(name="gmail-auth")
def gmail_auth_cmd(
    headless: bool = typer.Option(
        False,
        "--headless",
        help="Print auth URL and read code manually (for SSH/CI/no-browser envs)",
    ),
):
    """Run OAuth2 for Gmail sending (scope: gmail.send)."""
    from src import gmail_sender
    try:
        path = gmail_sender.run_local_auth(headless=headless)
    except Exception as e:
        typer.echo(f"Auth failed: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Saved Gmail token: {path}")
    typer.echo("Verify with: python -m src.cli watch-later-status")


@app.command(name="watch-later-sync")
def watch_later_sync_cmd(
    limit: int = typer.Option(25, "--limit", "-n", help="Max videos to process this run"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a single Watch Later poll + process + email pass and exit."""
    _ensure_db()
    try:
        get_api_key()
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    from src.watch_later_loop import sync_once
    report = sync_once(model=model, limit=limit)

    if verbose or report.error:
        import json as _json
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        typer.echo(
            f"Watch Later: discovered={report.discovered} processed={report.processed} "
            f"emailed={report.emailed} failed={report.failed}"
        )
    if report.error:
        raise typer.Exit(2)


@app.command(name="watch-later-status")
def watch_later_status_cmd():
    """Show Watch Later pipeline status (auth + loop + DB)."""
    from src.watch_later_loop import get_status
    import json as _json
    status = get_status()
    typer.echo(_json.dumps(status, indent=2))


@app.command(name="watch-later-list")
def watch_later_list_cmd(
    limit: int = typer.Option(20, "--limit", "-n"),
    only_pending: bool = typer.Option(False, "--pending"),
):
    """List Watch Later entries recorded by the pipeline."""
    _ensure_db()
    from src import db
    rows = (
        db.list_unprocessed_watch_later_entries()
        if only_pending
        else db.list_watch_later_entries(limit=limit)
    )
    if not rows:
        typer.echo("No Watch Later entries.")
        return
    for r in rows:
        marker = "OK " if r.get("emailed_at") else ("PEND" if not r.get("processed_at") else "FAIL")
        title = (r.get("video_title") or "(no title)")[:60]
        typer.echo(f"  [{marker}] {r['video_id']}  {title}")
        if r.get("last_error"):
            typer.echo(f"          error: {r['last_error'][:80]}")


@app.command(name="goals-validate")
def goals_validate_cmd(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to goals.yaml (default: search standard locations)"
    ),
):
    """Validate goals.yaml against the schema. Fails loud on bad config."""
    from src.goals import load_goals
    try:
        cfg = load_goals(config)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"FAIL: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"OK goals.yaml v{cfg.version} owner={cfg.owner} last_reviewed={cfg.last_reviewed}")
    typer.echo(f"  goals: {len(cfg.goals)}")
    for g in cfg.goals:
        typer.echo(
            f"    - {g.id:25s} priority={g.priority} auth={g.default_authority} "
            f"rubric={sorted(g.scoring_rubric.keys())} "
            f"required_evidence={g.constraints.required_evidence or '-'}"
        )
    typer.echo(f"  authority_tiers: {cfg.authority_tier_keys}")
    typer.echo(f"  atom_types: {len(cfg.atom_types)}")
    typer.echo(f"  atom_evidence: {len(cfg.atom_evidence)}")
    typer.echo(f"  tier_overrides: {len(cfg.tier_overrides)}")
    typer.echo(f"  required_action_fields: {len(cfg.output_contract.required_fields)}")
    typer.echo(f"  rejection_rules: {len(cfg.output_contract.rejection_rule_ids)}")


@app.command(name="stage2")
def stage2_cmd(
    video_id: str = typer.Argument(..., help="Video ID (11 chars) to re-score with current goals.yaml"),
    prompt_version: str = typer.Option(DEFAULT_PROMPT_VERSION, "--prompt-version", "-p"),
    send_email_flag: bool = typer.Option(False, "--send-email", help="Email the action brief"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Re-run Stage 2 against an existing v2 extraction using the current goals.yaml.

    Useful when goals.yaml changes and you want to re-score historical videos
    without re-transcribing.
    """
    _ensure_db()

    from src.stage2 import score_video, persist_stage2, load_goals
    from src import db

    extraction_row = db.get_analysis(video_id, prompt_version=prompt_version)
    if not extraction_row:
        typer.echo(f"No {prompt_version} analysis stored for {video_id}. Run `analyse --prompt-version {prompt_version}` first.", err=True)
        raise typer.Exit(1)
    extraction = extraction_row.get("output", {})
    if not extraction.get("transferable_atoms"):
        typer.echo(f"Stored {prompt_version} analysis has no transferable_atoms. Re-run analyse to produce a v2 extraction.", err=True)
        raise typer.Exit(1)

    cfg = load_goals()
    s2 = score_video(video_id=video_id, extraction=extraction, cfg=cfg, verbose=verbose)
    persist_stage2(video_id, s2, prompt_version=prompt_version)

    payload = s2.to_dict()
    max_rel = max((g.get("relevance", 0) for g in (payload.get("per_goal") or [])), default=0)
    typer.echo(f"video_id={video_id} max_relevance={max_rel}/3")
    for g in (payload.get("per_goal") or []):
        n_actions = len(g.get("proposed_actions") or [])
        if n_actions:
            typer.echo(f"  {g['goal_id']:25s} relevance={g['relevance']}  actions={n_actions}")
        else:
            typer.echo(f"  {g['goal_id']:25s} relevance={g['relevance']}  skip={g.get('skip_reason') or 'no actions shaped'}")
    n_rej = len(payload.get("rejections") or [])
    if n_rej:
        typer.echo(f"  rejections={n_rej} (see full JSON via `watch-later/entries` API or DB)")

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
        from src import gmail_sender
        sent = gmail_sender.send_analysis_email(result, stage2=payload)
        typer.echo(f"  [EMAIL] {sent['message_id']}")


def _parse_video_selection(videos_str: Optional[str], max_videos: int) -> list[int]:
    if not videos_str:
        return []

    indices = []
    for part in videos_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-', 1)
            start = int(start.strip())
            end = int(end.strip())
            indices.extend(range(start, end + 1))
        else:
            indices.append(int(part))

    indices = sorted(set(indices))
    indices = [i for i in indices if 1 <= i <= max_videos]

    if len(indices) > MAX_VIDEO_SELECT:
        typer.echo(f"Warning: Selected {len(indices)} videos, limiting to {MAX_VIDEO_SELECT}")
        indices = indices[:MAX_VIDEO_SELECT]

    return indices


def _prompt_video_selection(max_videos: int) -> list[int]:
    prompt = typer.prompt(
        f"Enter video numbers (comma-separated, max {MAX_VIDEO_SELECT})",
        default=""
    )

    if not prompt.strip():
        return []

    return _parse_video_selection(prompt, max_videos)


def _print_summary(results: list, output_dir: Path) -> None:
    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful

    typer.echo("\n" + "=" * 60)
    typer.echo("Summary")
    typer.echo("=" * 60)
    typer.echo(f"Total processed: {len(results)}")
    typer.echo(f"Successful: {successful}")
    typer.echo(f"Failed: {failed}")
    typer.echo(f"Output directory: {output_dir}")
    typer.echo(f"Knowledge base: {db.get_db_path()}")
    typer.echo("=" * 60)


if __name__ == "__main__":
    app()