import json
import re
from pathlib import Path
from src.summarizer import SummaryResult


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name[:100]


def save_summary(result: SummaryResult, output_dir: Path) -> tuple[Path, Path]:
    channel_safe = sanitize_filename(result.channel_name or "unknown")
    title_safe = sanitize_filename(result.video_title)

    base_filename = f"{channel_safe}_{title_safe}_{result.video_id}"

    json_path = output_dir / f"{base_filename}.json"
    md_path = output_dir / f"{base_filename}.md"

    _save_json(result, json_path)
    _save_markdown(result, md_path)

    return json_path, md_path


def _save_json(result: SummaryResult, path: Path) -> None:
    data = {
        "video_id": result.video_id,
        "video_title": result.video_title,
        "video_url": result.video_url,
        "channel_name": result.channel_name,
        "transcript_length": result.transcript_length,
        "chunks_used": result.chunks_used,
        "prompt_version": result.prompt_version,
        "success": result.success,
        "error": result.error,
        "tldr": result.tldr,
        "argument": result.argument,
        "key_concepts": result.key_concepts,
        "takeaways": result.takeaways,
        "claims_to_verify": result.claims_to_verify,
        "glossary": result.glossary,
        "markdown": result.markdown,
        "structured_output": result.structured_output,
    }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _save_markdown(result: SummaryResult, path: Path) -> None:
    if result.markdown:
        path.write_text(result.markdown, encoding='utf-8')
        return

    lines = []

    lines.append(f"# {result.video_title}")
    lines.append("")
    lines.append(f"**Channel:** {result.channel_name} | **URL:** [{result.video_url}]({result.video_url})")
    lines.append("")

    if result.error and not result.success:
        lines.append(f"⚠️ **Error:** {result.error}")
        lines.append("")

    lines.append("## TL;DR")
    lines.append("")
    lines.append(result.tldr or result.summary or "_No summary available_")
    lines.append("")

    if result.argument:
        lines.append("## The Argument")
        lines.append("")
        lines.append(result.argument)
        lines.append("")

    if result.key_concepts:
        lines.append("## Key Concepts")
        lines.append("")
        for c in result.key_concepts:
            ts = _format_ts(c.get('first_timestamp_seconds'))
            lines.append(f"- **{c.get('name', '')}** — {c.get('definition', '')} _(at {ts})_")
        lines.append("")

    if result.takeaways:
        lines.append("## Actionable Takeaways")
        lines.append("")
        by_cat: dict[str, list[str]] = {}
        for t in result.takeaways:
            cat = t.get('category', 'general')
            by_cat.setdefault(cat, []).append(t.get('text', ''))
        for cat, items in by_cat.items():
            lines.append(f"### {cat.title()}")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    if result.claims_to_verify:
        lines.append("## Claims to Verify")
        lines.append("")
        for claim in result.claims_to_verify:
            ts = _format_ts(claim.get('timestamp_seconds'))
            lines.append(f"- {claim.get('text', '')} _(at {ts})_")
        lines.append("")

    if result.glossary:
        lines.append("## Glossary")
        lines.append("")
        for entity in result.glossary:
            lines.append(f"- **{entity.get('name', '')}** ({entity.get('type', 'entity')})")
        lines.append("")

    path.write_text('\n'.join(lines), encoding='utf-8')


def _format_ts(seconds) -> str:
    if seconds is None:
        return "??:??"
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "??:??"
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def print_summary_result(result: SummaryResult) -> None:
    print("\n" + "=" * 60)
    print(f"✓ {result.video_title}")
    print(f"  Channel: {result.channel_name}")
    print(f"  Prompt: {result.prompt_version}")
    print("=" * 60)

    if result.success:
        if result.tldr:
            print(f"\n{result.tldr}")
        print(f"\nKey concepts: {len(result.key_concepts)} | "
              f"Takeaways: {len(result.takeaways)} | "
              f"Claims to verify: {len(result.claims_to_verify)}")
    else:
        print(f"\n⚠ Error: {result.error}")

    print()