from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from src import prompts as prompts_pkg
from src.chunker import chunk_transcript_preserve_context
from src.config import get_api_key, DEFAULT_MODEL
from src.embeddings import embed_texts


@dataclass
class SummaryResult:
    video_id: str
    video_title: str
    video_url: str
    channel_name: str
    summary: str
    key_points: list[str]
    important_links: list[dict, str]
    timestamp_topics: list[dict, str]
    transcript_length: int
    chunks_used: int
    success: bool
    error: Optional[str] = None
    prompt_version: str = "v1"
    tldr: str = ""
    argument: str = ""
    key_concepts: list[dict] = field(default_factory=list)
    takeaways: list[dict] = field(default_factory=list)
    claims_to_verify: list[dict] = field(default_factory=list)
    glossary: list[dict] = field(default_factory=list)
    markdown: str = ""
    structured_output: dict = field(default_factory=dict)
    chunk_extractions: list[dict] = field(default_factory=list)
    # v2 additions
    atoms: list[dict] = field(default_factory=list)
    stack: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    thesis: str = ""


def _extract_json(content: str) -> Optional[dict]:
    text = content.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Markdown fallback extractors (v2 reduce prompt sometimes puts atoms inside
# the markdown field instead of as top-level JSON fields)
# ---------------------------------------------------------------------------


_ATOM_LINE_RE = re.compile(
    r"^[*-]\s*`?(atom_\d+)`?\s*[—–-]\s*(.+?)(?:\s*@\s*([0-9:]+(?:\s*-\s*[0-9:]+)?))?\s*$",
    re.MULTILINE,
)
_ATOM_TYPES = {
    "implementation_pattern", "framework", "org_pattern", "business_model",
    "revenue_pattern", "architecture", "tool_recipe", "concept",
}
_EVIDENCE_TYPES = {"stated_practice", "framework_claim", "anecdotal", "data"}


def _split_md_sections(md: str) -> dict[str, str]:
    """Return markdown split into sections keyed by '## Atoms', '## Stack', etc."""
    out: dict[str, str] = {}
    current = ""
    current_key = "_preamble"
    for line in md.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            out[current_key] = current
            current_key = h.group(1).strip().lower()
            current = ""
        else:
            current += line + "\n"
    out[current_key] = current
    return out


def _extract_atoms_from_markdown(md: str) -> list[dict]:
    if not md:
        return []
    sections = _split_md_sections(md)
    body = sections.get("atoms", "")
    if not body.strip():
        return []
    atoms: list[dict] = []
    idx = 0
    for m in _ATOM_LINE_RE.finditer(body):
        atom_id, label, ts = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        label = re.sub(r"^`+|`+$", "", label).strip()
        if not label:
            continue
        idx += 1
        atom = {
            "id": atom_id,
            "label": label[:200],
            "mechanism": label,
            "type": "concept",
            "evidence": "anecdotal",
            "dependencies": [],
            "timestamp": ts or "",
        }
        atoms.append(atom)
    return atoms


def _extract_stack_from_markdown(md: str) -> list[dict]:
    if not md:
        return []
    sections = _split_md_sections(md)
    body = sections.get("stack", "")
    if not body.strip():
        return []
    items: list[dict] = []
    for line in body.splitlines():
        m = re.match(
            r"^[*-]\s*(?:`([^`]+)`|([^\s—–-]+))\s*(?:[:—–-])\s*(.+)$",
            line.strip(),
        )
        if not m:
            continue
        tool = (m.group(1) or m.group(2) or "").strip().strip("`")
        role = m.group(3).strip()
        if not tool or tool == role:
            continue
        items.append({"tool": tool[:200], "role": role[:300]})
    return items


def _extract_open_questions_from_markdown(md: str) -> list[str]:
    if not md:
        return []
    sections = _split_md_sections(md)
    body = sections.get("open questions", "")
    if not body.strip():
        return []
    out: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^[*-]\s*(.+)$", line.strip())
        if m:
            out.append(m.group(1).strip()[:500])
    return out[:10]


def _extract_thesis_from_markdown(md: str) -> str:
    if not md:
        return ""
    m = re.search(r"\*\*Thesis:\*\*\s*(.+?)(?:\n|$)", md)
    if m:
        return m.group(1).strip()[:500]
    return ""


def _format_ts(seconds: Optional[float]) -> str:
    if seconds is None:
        return "??:??"
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "??:??"
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def summarize_transcript(
    transcript_text: str,
    video_title: str,
    video_url: str,
    channel_name: str,
    video_id: str,
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
    prompt_version: Optional[str] = None,
    segments: Optional[list[dict]] = None,
) -> SummaryResult:
    if not transcript_text or not transcript_text.strip():
        return SummaryResult(
            video_id=video_id,
            video_title=video_title,
            video_url=video_url,
            channel_name=channel_name,
            summary="",
            key_points=[],
            important_links=[],
            timestamp_topics=[],
            transcript_length=0,
            chunks_used=0,
            success=False,
            error="Empty transcript",
        )

    pv = prompts_pkg.resolve_version(prompt_version)
    api_key = get_api_key()

    chunks = chunk_transcript_preserve_context(transcript_text)
    total_chunks = len(chunks)
    transcript_length = len(transcript_text)

    if verbose:
        print(f"Prompt version: {pv}")
        print(f"Transcript length: {transcript_length} chars, {total_chunks} chunks")

    seg_starts = [seg.get('start', 0.0) for seg in (segments or [])]
    seg_texts = [seg.get('text', '') for seg in (segments or [])]

    chunk_records: list[dict] = []
    chunk_extractions: list[dict] = []

    for i, chunk in enumerate(chunks):
        char_start = sum(len(chunks[j]['text']) + 2 for j in range(i))
        char_end = char_start + len(chunk['text'])
        start_ts = seg_starts[0] if seg_starts and i == 0 else None
        end_ts = None

        chunk_records.append({
            'chunk_index': chunk['chunk_index'],
            'text': chunk['text'],
            'char_start': char_start,
            'char_end': char_end,
            'start_ts': start_ts,
            'end_ts': end_ts,
        })

        if verbose:
            print(f"  Map pass chunk {i + 1}/{total_chunks}...")

        part_label = f"{chunk['chunk_index'] + 1} of {chunk['total_chunks']}"
        map_template = prompts_pkg.load_prompt(pv, 'map')
        map_prompt = map_template.format(
            part_label=part_label,
            total_chunks=total_chunks,
            title=video_title,
            channel=channel_name,
            start_ts=_format_ts(start_ts),
            end_ts=_format_ts(end_ts),
            transcript=chunk['text'][:45000],
        )

        response = _call_minimax_api(api_key, model, map_prompt, verbose)
        if not response["success"]:
            chunk_extractions.append({
                'chunk_index': chunk['chunk_index'],
                'error': response.get("error"),
                'extraction': {'claims': [], 'concepts': [], 'examples': [], 'actions': [], 'entities': []},
            })
            continue

        parsed = _extract_json(response["content"]) or {}
        parsed.setdefault('claims', [])
        parsed.setdefault('concepts', [])
        parsed.setdefault('examples', [])
        parsed.setdefault('actions', [])
        parsed.setdefault('entities', [])

        chunk_extractions.append({
            'chunk_index': chunk['chunk_index'],
            'extraction': parsed,
        })

        if i < total_chunks - 1:
            time.sleep(0.5)

    reduce_template = prompts_pkg.load_prompt(pv, 'reduce')

    if pv == "v2":
        ts_lines = []
        for seg in (segments or [])[:3000]:
            try:
                ts = float(seg.get("start", 0.0))
            except (TypeError, ValueError):
                continue
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            ts_lines.append(f"{_format_ts(ts)}  {text}")
        transcript_with_timestamps = "\n".join(ts_lines)[:80000] or "(no timestamped segments available)"
        reduce_kwargs = dict(
            title=video_title,
            channel=channel_name,
            duration="unknown",
            url=video_url,
            n_chunks=total_chunks,
            transcript_length=transcript_length,
            chunk_extractions=json.dumps(chunk_extractions, ensure_ascii=False, indent=1)[:60000],
            transcript_with_timestamps=transcript_with_timestamps,
        )
    else:
        reduce_kwargs = dict(
            title=video_title,
            channel=channel_name,
            duration="unknown",
            url=video_url,
            n_chunks=total_chunks,
            transcript_length=transcript_length,
            chunk_extractions=json.dumps(chunk_extractions, ensure_ascii=False, indent=1)[:80000],
        )
    reduce_prompt = reduce_template.format(**reduce_kwargs)

    if verbose:
        print(f"  Reduce pass...")

    reduce_response = _call_minimax_api(api_key, model, reduce_prompt, verbose)
    if not reduce_response["success"]:
        return SummaryResult(
            video_id=video_id,
            video_title=video_title,
            video_url=video_url,
            channel_name=channel_name,
            summary="",
            key_points=[],
            important_links=[],
            timestamp_topics=[],
            transcript_length=transcript_length,
            chunks_used=total_chunks,
            success=False,
            error=f"Reduce pass failed: {reduce_response.get('error')}",
            prompt_version=pv,
            chunk_extractions=chunk_extractions,
        )

    final = _extract_json(reduce_response["content"])
    if final is None:
        return SummaryResult(
            video_id=video_id,
            video_title=video_title,
            video_url=video_url,
            channel_name=channel_name,
            summary=reduce_response["content"][:1000],
            key_points=[],
            important_links=[],
            timestamp_topics=[],
            transcript_length=transcript_length,
            chunks_used=total_chunks,
            success=True,
            error="Could not parse reduce JSON",
            prompt_version=pv,
            chunk_extractions=chunk_extractions,
        )

    final.setdefault('tldr', '')
    final.setdefault('argument', '')
    final.setdefault('key_concepts', [])
    final.setdefault('takeaways', [])
    final.setdefault('claims_to_verify', [])
    final.setdefault('glossary', [])
    final.setdefault('markdown', '')

    if pv == "v2":
        if not final.get("transferable_atoms"):
            final["transferable_atoms"] = _extract_atoms_from_markdown(final.get("markdown", ""))
        if not final.get("stack"):
            final["stack"] = _extract_stack_from_markdown(final.get("markdown", ""))
        if not final.get("open_questions"):
            final["open_questions"] = _extract_open_questions_from_markdown(final.get("markdown", ""))
        if not final.get("thesis"):
            final["thesis"] = _extract_thesis_from_markdown(final.get("markdown", ""))

    key_points = [c.get('name', '') + ': ' + c.get('definition', '') for c in final['key_concepts']]
    timestamp_topics = [
        {"timestamp": _format_ts(c.get('first_timestamp_seconds')), "topic": c.get('name', '')}
        for c in final['key_concepts']
    ]

    atoms = final.get('transferable_atoms', []) or []
    stack = final.get('stack', []) or []
    open_questions = final.get('open_questions', []) or []
    meta = final.get('meta', {}) or {}
    thesis = final.get('thesis', '') or final.get('tldr', '')

    return SummaryResult(
        video_id=video_id,
        video_title=video_title,
        video_url=video_url,
        channel_name=channel_name,
        summary=final['tldr'] or final.get('summary', ''),
        key_points=key_points,
        important_links=[],
        timestamp_topics=timestamp_topics,
        transcript_length=transcript_length,
        chunks_used=total_chunks,
        success=True,
        prompt_version=pv,
        tldr=final.get('tldr', ''),
        argument=final.get('argument', ''),
        key_concepts=final.get('key_concepts', []),
        takeaways=final.get('takeaways', []),
        claims_to_verify=final.get('claims_to_verify', []),
        glossary=final.get('glossary', []),
        markdown=final.get('markdown', ''),
        structured_output=final,
        chunk_extractions=chunk_extractions,
        atoms=atoms,
        stack=stack,
        open_questions=open_questions,
        thesis=thesis,
    )


def embed_chunks(texts: list[str], model_name: str = "all-MiniLM-L6-v2") -> np.ndarray:
    return embed_texts(texts, model_name=model_name)


def _call_minimax_api(api_key: str, model: str, prompt: str, verbose: bool, max_retries: int = 3) -> dict:
    url = "https://api.minimaxi.chat/v1/text/chatcompletion_v2"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
    }

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"  API call attempt {attempt + 1}/{max_retries}")

            response = requests_post(url, headers=headers, json=payload, timeout=180)

            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"success": True, "content": content}

            elif response.status_code == 429:
                wait_time = (attempt + 1) * 2
                if verbose:
                    print(f"  Rate limited, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue

            else:
                error_msg = f"API error: {response.status_code} - {response.text[:200]}"
                if verbose:
                    print(f"  {error_msg}")
                return {"success": False, "error": error_msg}

        except Exception as e:
            if verbose:
                print(f"  Error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "Max retries exceeded"}


def requests_post(url, headers, json, timeout):
    import requests
    return requests.post(url, headers=headers, json=json, timeout=timeout)