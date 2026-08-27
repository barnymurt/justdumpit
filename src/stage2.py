"""Stage 2: goal-conditioned scoring of Stage 1 atoms.

Reads config/goals.yaml, builds a single LLM prompt that scores each
goal against the video's atoms + thesis + stack, validates the response
against the action_output_contract from goals.yaml, applies tier
overrides, and emits a structured Stage2Result with rejections
audited.

Goal-agnostic Stage 1 means changing goals.yaml + re-running Stage 2
on existing v2 analyses re-evaluates everything without re-transcribing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src import db as db_mod
from src.goals import (
    ATOM_EVIDENCE,
    ATOM_TYPES,
    AUTHORITY_TIER_KEYS,
    IMPACT_VALUES,
    REVERSIBILITY_VALUES,
    TIER_ORDER,
    GoalsConfig,
    Goal,
    load_goals,
)
from src.config import DEFAULT_MODEL


log = logging.getLogger("ytscraper.stage2")


STAGE2_MODEL = os.getenv("STAGE2_MODEL", "").strip() or DEFAULT_MODEL
STAGE2_ENABLED = os.getenv("STAGE2_ENABLED", "true").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass
class ProposedAction:
    goal_id: str
    relevance_score: int
    atoms_used: list[str]
    action_description: str
    proposed_tier: str
    effort_estimate_hours: int
    reversibility: str
    external_surface: bool
    dependencies: list[str]
    impact_classification: Optional[str] = None
    pre_check: Optional[list[str]] = None


@dataclass
class GoalResult:
    goal_id: str
    goal_name: str
    relevance: int
    applicable_atoms: list[str]
    proposed_actions: list[ProposedAction] = field(default_factory=list)
    skip_reason: Optional[str] = None
    raw_response: Optional[dict] = None


@dataclass
class Rejection:
    action_id: str
    rule_id: str
    reason: str
    suggested_next_step: str
    logged_at: str


@dataclass
class Stage2Result:
    video_id: str
    prompt_version: str
    goals_version: int
    owner: str
    completed_at: str
    per_goal: list[GoalResult]
    rejections: list[Rejection]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "prompt_version": self.prompt_version,
            "goals_version": self.goals_version,
            "owner": self.owner,
            "completed_at": self.completed_at,
            "per_goal": [
                {
                    "goal_id": g.goal_id,
                    "goal_name": g.goal_name,
                    "relevance": g.relevance,
                    "applicable_atoms": g.applicable_atoms,
                    "proposed_actions": [
                        {
                            "goal_id": a.goal_id,
                            "relevance_score": a.relevance_score,
                            "atoms_used": a.atoms_used,
                            "action_description": a.action_description,
                            "proposed_tier": a.proposed_tier,
                            "effort_estimate_hours": a.effort_estimate_hours,
                            "reversibility": a.reversibility,
                            "external_surface": a.external_surface,
                            "dependencies": a.dependencies,
                            "impact_classification": a.impact_classification,
                            "pre_check": a.pre_check,
                        }
                        for a in g.proposed_actions
                    ],
                    "skip_reason": g.skip_reason,
                }
                for g in self.per_goal
            ],
            "rejections": [
                {
                    "action_id": r.action_id,
                    "rule_id": r.rule_id,
                    "reason": r.reason,
                    "suggested_next_step": r.suggested_next_step,
                    "logged_at": r.logged_at,
                }
                for r in self.rejections
            ],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _find_balanced_array(text: str, start: int) -> Optional[tuple[int, int]]:
    """Find the matching closing bracket for the '[' at position start.

    Walks the string tracking nesting, ignoring brackets inside string literals.
    Returns (start, end) inclusive or None if unbalanced.
    """
    if start < 0 or text[start] != "[":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _extract_json_array(content: str) -> Optional[list]:
    text = content.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for k in ("per_goal", "results", "goal_results", "data"):
                if k in parsed and isinstance(parsed[k], list):
                    return parsed[k]
    except json.JSONDecodeError:
        pass

    for m in _JSON_FENCE_RE.finditer(text):
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    for start_idx in [text.find("["), 0]:
        if start_idx < 0:
            continue
        balanced = _find_balanced_array(text, start_idx)
        if balanced is None:
            continue
        s, e = balanced
        candidate = text[s:e + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(extraction: dict, cfg: GoalsConfig) -> str:
    atoms = extraction.get("transferable_atoms", []) or []
    stack = extraction.get("stack", []) or []
    thesis = extraction.get("thesis", "") or extraction.get("tldr", "")
    meta = extraction.get("meta", {}) or {}

    atoms_block = "\n".join(
        f"- id={a.get('id','')}\n"
        f"  label: {a.get('label','')}\n"
        f"  type: {a.get('type','')}\n"
        f"  evidence: {a.get('evidence','')}\n"
        f"  mechanism: {a.get('mechanism','')}\n"
        f"  dependencies: {a.get('dependencies', []) or []}\n"
        f"  timestamp: {a.get('timestamp','')}"
        for a in atoms
    ) or "(no atoms extracted)"

    stack_block = "\n".join(
        f"- {s.get('tool','')}: {s.get('role','')}"
        for s in stack
    ) or "(no stack mentioned)"

    goals_block_parts = []
    for g in cfg.goals:
        rubric_text = "\n".join(
            f"      {score}: {desc.strip()}"
            for score, desc in sorted(g.scoring_rubric.items(), reverse=True)
        )
        applies = "; ".join(g.applies_to) or "(none specified)"
        excludes = "; ".join(g.excludes) or "(none specified)"
        req = g.constraints.required_evidence or "(none)"
        goals_block_parts.append(
            f"  - id: {g.id}\n"
            f"    priority: {g.priority}\n"
            f"    name: {g.name}\n"
            f"    default_authority: {g.default_authority}\n"
            f"    applies_to: {applies}\n"
            f"    excludes: {excludes}\n"
            f"    required_evidence: {req}\n"
            f"    scoring_rubric:\n{rubric_text}"
        )
    goals_block = "\n".join(goals_block_parts)

    pre_check_block_list = cfg.pre_check_block or []
    pre_check_block = "\n".join(f"      - {q}" for q in pre_check_block_list) or "      (none)"

    return f"""You are Stage 2 of a video-to-action pipeline. Stage 1 has already extracted goal-agnostic atoms from a video. Your job: score each goal against the atoms, and propose concrete, action-shaped work the operator can evaluate. ONLY propose what the atoms support. If nothing scores high enough, return skip_reason and no actions. Do not invent.

# Stage 1 extraction

video_title: {meta.get('title', '')}
video_url: {meta.get('url', '')}
channel: {meta.get('channel', '')}
duration_seconds: {meta.get('duration_seconds', None)}
thesis: {thesis}

transferable_atoms:
{atoms_block}

stack:
{stack_block}

# Goals

The operator has these goals (in priority order). Score each one:

{goals_block}

# Authority tiers

  tier_0_auto: fully reversible, local-only, no external surface. Agent executes without notification.
  tier_1_auto_with_notification: reversible via a single undo. Agent executes and posts a summary.
  tier_2_propose_with_artifact: agent produces a written proposal and posts it for the operator to green-light.
  tier_3_explicit_green_light: as tier_2 plus a mandatory pre-check block. Operator must explicitly approve.
  tier_4_hard_stop: agent must never execute; emit only as a flagged proposal.

Tier overrides applied after default_authority (higher tier wins):
  - touches_live_product_owned_by_me AND impact == substantial → tier_3
  - touches_live_product_owned_by_me AND impact == minor → tier_2
  - touches_client_or_third_party_work → tier_3
  - publishes_public_content_under_my_name → tier_3
  - incurs_any_monetary_cost → tier_3
  - modifies_secrets_or_credentials → tier_4
  - creates_or_modifies_legal_or_financial_commitment → tier_4

# Pre-check block (required for any action at tier_3 or tier_4)

{pre_check_block}

# Output contract

Return STRICT JSON, no commentary, no markdown fences. A single array — one entry per goal in the order listed above:

[
  {{
    "goal_id": "dev_workflow",
    "relevance_score": 2,
    "rationale": "one sentence, why this score against the rubric",
    "applicable_atoms": ["atom_01", "atom_02"],
    "proposed_actions": [
      {{
        "action_id": "act_01",
        "action_description": "what will actually happen, in one paragraph. Concrete enough that the operator can paste it into a GitHub issue and a Claude Code agent can execute it.",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_2_propose_with_artifact",
        "effort_estimate_hours": 4,
        "reversibility": "undo_able",
        "external_surface": false,
        "dependencies": ["existing GitHub repo access", "OpenAI API key on file"],
        "impact_classification": "minor",
        "pre_check": null
      }}
    ],
    "skip_reason": null
  }},
  {{
    "goal_id": "side_income",
    "relevance_score": 1,
    "rationale": "...",
    "applicable_atoms": [],
    "proposed_actions": [],
    "skip_reason": "atoms present don't describe a revenue model; highest-scoring atom is X which is a framework, not a business model"
  }}
]

# Rules

- relevance_score must be 0, 1, 2, or 3 per the goal's scoring_rubric.
- If relevance_score < 2: proposed_actions MUST be empty and skip_reason MUST be non-null.
- If relevance_score >= 2: applicable_atoms lists the atoms that drove the score; proposed_actions may be empty ONLY if no concrete action can be shaped from those atoms.
- action_description: one paragraph, concrete, references the specific atoms and what changes.
- proposed_tier: must be one of: tier_0_auto, tier_1_auto_with_notification, tier_2_propose_with_artifact, tier_3_explicit_green_light, tier_4_hard_stop. Start from the goal's default_authority and bump up if tier_overrides apply.
- effort_estimate_hours: integer.
- reversibility: must be one of: trivial, undo_able, hard, irreversible.
- external_surface: boolean — does this touch anyone outside the operator?
- dependencies: list of strings naming things outside the agent's control (tools, accounts, third-party services). Empty list if none.
- impact_classification: "substantial" or "minor" if the action touches a live product the operator owns. Otherwise null.
- pre_check: list of strings answering each pre-check question, or null if tier < 3.
- Atom types: implementation_pattern, framework, org_pattern, business_model, revenue_pattern, architecture, tool_recipe, concept.
- Atom evidence: stated_practice, framework_claim, anecdotal, data.

Output ONLY the JSON array, no preamble.
"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_action(action: ProposedAction, cfg: GoalsConfig, log: list[Rejection]) -> bool:
    """Apply rejection_rules from goals.yaml. Returns True if action passes."""
    aid = f"{action.goal_id}:{action.atoms_used[0] if action.atoms_used else 'no_atoms'}"
    now = _now_iso()

    if action.relevance_score < 2:
        log.append(Rejection(
            action_id=aid,
            rule_id="low_relevance",
            reason=f"goal {action.goal_id}: relevance {action.relevance_score} < 2",
            suggested_next_step="skip_or_reprocess_stage_1",
            logged_at=now,
        ))
        return False

    if action.proposed_tier not in AUTHORITY_TIER_KEYS:
        log.append(Rejection(
            action_id=aid,
            rule_id="invalid_tier",
            reason=f"proposed_tier {action.proposed_tier!r} not in {sorted(AUTHORITY_TIER_KEYS)}",
            suggested_next_step="agent_retires_with_valid_tier",
            logged_at=now,
        ))
        return False

    if action.reversibility not in REVERSIBILITY_VALUES:
        log.append(Rejection(
            action_id=aid,
            rule_id="invalid_reversibility",
            reason=f"reversibility {action.reversibility!r} not in {sorted(REVERSIBILITY_VALUES)}",
            suggested_next_step="agent_retires_with_valid_reversibility",
            logged_at=now,
        ))
        return False

    if action.impact_classification is not None and action.impact_classification not in IMPACT_VALUES:
        log.append(Rejection(
            action_id=aid,
            rule_id="invalid_impact",
            reason=f"impact_classification {action.impact_classification!r} not in {sorted(IMPACT_VALUES)}",
            suggested_next_step="agent_retires_with_valid_impact",
            logged_at=now,
        ))
        return False

    tier_num = TIER_ORDER[action.proposed_tier]
    if action.reversibility == "irreversible" and tier_num < TIER_ORDER["tier_4_hard_stop"]:
        log.append(Rejection(
            action_id=aid,
            rule_id="irreversible_without_hard_stop",
            reason=f"reversibility=irreversible but proposed at {action.proposed_tier}",
            suggested_next_step="escalate_to_human_for_reclassification",
            logged_at=now,
        ))
        return False

    if tier_num >= TIER_ORDER["tier_3_explicit_green_light"] and not action.pre_check:
        log.append(Rejection(
            action_id=aid,
            rule_id="missing_pre_check",
            reason=f"tier {action.proposed_tier} requires pre_check but none provided",
            suggested_next_step="agent_completes_pre_check_or_drops_action",
            logged_at=now,
        ))
        return False

    if tier_num == TIER_ORDER["tier_0_auto"] and action.external_surface:
        log.append(Rejection(
            action_id=aid,
            rule_id="external_surface_at_auto_tier",
            reason="tier_0_auto cannot touch external surfaces",
            suggested_next_step="bump_tier_and_repropose",
            logged_at=now,
        ))
        return False

    if not action.action_description or len(action.action_description.strip()) < 20:
        log.append(Rejection(
            action_id=aid,
            rule_id="insufficient_action_description",
            reason="action_description missing or too short (<20 chars)",
            suggested_next_step="agent_expands_description",
            logged_at=now,
        ))
        return False

    for atom_id in action.atoms_used:
        if not atom_id.startswith("atom_"):
            log.append(Rejection(
                action_id=aid,
                rule_id="invalid_atom_reference",
                reason=f"atoms_used contains non-atom id: {atom_id!r}",
                suggested_next_step="agent_corrects_atoms_used",
                logged_at=now,
            ))
            return False

    return True


def _parse_action(action_dict: dict, goal_id: str) -> ProposedAction:
    return ProposedAction(
        goal_id=goal_id,
        relevance_score=int(action_dict.get("relevance_score", 0)),
        atoms_used=list(action_dict.get("atoms_used", []) or []),
        action_description=str(action_dict.get("action_description", "")),
        proposed_tier=str(action_dict.get("proposed_tier", "tier_2_propose_with_artifact")),
        effort_estimate_hours=int(action_dict.get("effort_estimate_hours", 1)),
        reversibility=str(action_dict.get("reversibility", "undo_able")),
        external_surface=bool(action_dict.get("external_surface", False)),
        dependencies=list(action_dict.get("dependencies", []) or []),
        impact_classification=action_dict.get("impact_classification"),
        pre_check=action_dict.get("pre_check"),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def score_video(
    video_id: str,
    extraction: dict,
    cfg: Optional[GoalsConfig] = None,
    model: str = STAGE2_MODEL,
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> Stage2Result:
    """Score a v2 extraction against goals.yaml. Returns structured Stage2Result."""
    if not STAGE2_ENABLED:
        return Stage2Result(
            video_id=video_id,
            prompt_version=extraction.get("meta", {}).get("prompt_version", "v2"),
            goals_version=0,
            owner="",
            completed_at=_now_iso(),
            per_goal=[],
            rejections=[],
            error="STAGE2_ENABLED is false",
        )

    if cfg is None:
        cfg = load_goals()

    if not extraction.get("transferable_atoms"):
        return Stage2Result(
            video_id=video_id,
            prompt_version="v2",
            goals_version=cfg.version,
            owner=cfg.owner,
            completed_at=_now_iso(),
            per_goal=[
                GoalResult(
                    goal_id=g.id,
                    goal_name=g.name,
                    relevance=0,
                    applicable_atoms=[],
                    skip_reason="no atoms extracted",
                )
                for g in cfg.goals
            ],
            rejections=[],
            error=None,
        )

    from src.config import get_api_key
    api_key = api_key or get_api_key()
    prompt = _build_prompt(extraction, cfg)
    response = _call_minimax_api(api_key, model, prompt, verbose)
    if not response["success"]:
        return Stage2Result(
            video_id=video_id,
            prompt_version="v2",
            goals_version=cfg.version,
            owner=cfg.owner,
            completed_at=_now_iso(),
            per_goal=[],
            rejections=[],
            error=f"LLM call failed: {response.get('error')}",
        )

    raw_array = _extract_json_array(response["content"])
    if not raw_array:
        retry_prompt = (
            prompt
            + "\n\n---\n\nYour previous response could not be parsed as JSON. "
            "Respond with ONLY the JSON array, no commentary, no markdown fences. "
            "Make sure every [ has a matching ], every string is properly quoted, and the entire response parses with json.loads."
        )
        retry_response = _call_minimax_api(api_key, model, retry_prompt, verbose)
        if retry_response["success"]:
            raw_array = _extract_json_array(retry_response["content"])
            if raw_array:
                log.warning("Stage 2 parse succeeded on retry for %s", video_id)

    if not raw_array:
        return Stage2Result(
            video_id=video_id,
            prompt_version="v2",
            goals_version=cfg.version,
            owner=cfg.owner,
            completed_at=_now_iso(),
            per_goal=[],
            rejections=[],
            error="could not parse Stage 2 JSON array",
        )

    rejections: list[Rejection] = []
    per_goal: list[GoalResult] = []

    for entry in raw_array:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("goal_id", "")
        goal = next((g for g in cfg.goals if g.id == gid), None)
        if goal is None:
            rejections.append(Rejection(
                action_id=gid or "unknown",
                rule_id="unknown_goal_id",
                reason=f"goal_id {gid!r} not in goals.yaml",
                suggested_next_step="agent_corrects_goal_id",
                logged_at=_now_iso(),
            ))
            continue

        try:
            relevance = int(entry.get("relevance_score", 0))
        except (TypeError, ValueError):
            relevance = 0

        applicable_atoms = list(entry.get("applicable_atoms", []) or [])
        skip_reason = entry.get("skip_reason")
        proposed: list[ProposedAction] = []

        if relevance < 2:
            skip_reason = skip_reason or "relevance < 2; no actions"
        else:
            for raw_action in entry.get("proposed_actions", []) or []:
                if not isinstance(raw_action, dict):
                    continue
                action = _parse_action(raw_action, gid)
                if _validate_action(action, cfg, rejections):
                    proposed.append(action)

        per_goal.append(GoalResult(
            goal_id=gid,
            goal_name=goal.name,
            relevance=relevance,
            applicable_atoms=applicable_atoms,
            proposed_actions=proposed,
            skip_reason=skip_reason,
            raw_response=None,
        ))

    return Stage2Result(
        video_id=video_id,
        prompt_version="v2",
        goals_version=cfg.version,
        owner=cfg.owner,
        completed_at=_now_iso(),
        per_goal=per_goal,
        rejections=rejections,
    )


# ---------------------------------------------------------------------------
# LLM call (mirrors summarizer._call_minimax_api but inline)
# ---------------------------------------------------------------------------


def _call_minimax_api(api_key: str, model: str, prompt: str, verbose: bool, max_retries: int = 3) -> dict:
    import requests

    url = "https://api.minimaxi.chat/v1/text/chatcompletion_v2"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"  Stage 2 LLM attempt {attempt + 1}/{max_retries}")
            response = requests.post(url, headers=headers, json=payload, timeout=180)
            if response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"success": True, "content": content}
            if response.status_code == 429:
                wait = (attempt + 1) * 2
                if verbose:
                    print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return {"success": False, "error": str(e)}
    return {"success": False, "error": "Max retries exceeded"}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def persist_stage2(video_id: str, result: Stage2Result, prompt_version: str = "v2") -> None:
    db_mod.upsert_stage2_output(video_id, prompt_version, result.to_dict())


def load_stage2(video_id: str, prompt_version: str = "v2") -> Optional[dict]:
    return db_mod.get_stage2_output(video_id, prompt_version)
