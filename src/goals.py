"""Goals config loader and validator.

The goals.yaml file drives Stage 2 of the video-to-action pipeline.
Each goal has a scoring rubric, scope constraints, and a default
authority tier. The loader validates the structure at startup and
at `cli goals-validate` time so bad configs fail deploys fast.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


ATOM_TYPES: set[str] = {
    "implementation_pattern",
    "framework",
    "org_pattern",
    "business_model",
    "revenue_pattern",
    "architecture",
    "tool_recipe",
    "concept",
}

ATOM_EVIDENCE: set[str] = {
    "stated_practice",
    "framework_claim",
    "anecdotal",
    "data",
}

AUTHORITY_TIER_KEYS: set[str] = {
    "tier_0_auto",
    "tier_1_auto_with_notification",
    "tier_2_propose_with_artifact",
    "tier_3_explicit_green_light",
    "tier_4_hard_stop",
}

TIER_ORDER: dict[str, int] = {
    "tier_0_auto": 0,
    "tier_1_auto_with_notification": 1,
    "tier_2_propose_with_artifact": 2,
    "tier_3_explicit_green_light": 3,
    "tier_4_hard_stop": 4,
}

REVERSIBILITY_VALUES: set[str] = {"trivial", "undo_able", "hard", "irreversible"}

IMPACT_VALUES: set[str] = {"substantial", "minor"}


@dataclass
class GoalConstraints:
    required_evidence: Optional[str] = None
    max_effort_per_action_hours: Optional[int] = None
    exploration_bonus: Optional[str] = None


@dataclass
class Goal:
    id: str
    name: str
    priority: int
    description: str
    scoring_rubric: dict[int, str]
    constraints: GoalConstraints
    default_authority: str
    applies_to: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


@dataclass
class AuthorityTiers:
    descriptions: dict[str, str] = field(default_factory=dict)
    pre_check_block: list[str] = field(default_factory=list)


@dataclass
class ActionOutputContract:
    required_fields: list[str]
    rejection_rule_ids: list[str]


@dataclass
class GoalsConfig:
    version: int
    owner: str
    last_reviewed: str
    goals: list[Goal]
    authority_tier_keys: list[str]
    pre_check_block: list[str]
    output_contract: ActionOutputContract
    atom_types: list[str]
    atom_evidence: list[str]
    tier_overrides: list[dict]
    impact_classification: dict


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    """Locate goals.yaml. Check CONFIG_PATH env first, then standard locations."""
    env = os.getenv("GOALS_CONFIG_PATH", "").strip()
    if env:
        return Path(env)
    candidates = [
        Path(__file__).parent.parent / "config" / "goals.yaml",
        Path("/app/config/goals.yaml"),
        Path("/data/goals.yaml"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"goals.yaml not found. Tried: {[str(p) for p in candidates]}. "
        f"Set GOALS_CONFIG_PATH or place goals.yaml in config/."
    )


def load_goals(path: Optional[Path] = None) -> GoalsConfig:
    """Load and validate goals.yaml. Raises ValueError on schema problems."""
    p = path or _config_path()
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    errors: list[str] = []

    if not isinstance(raw, dict):
        raise ValueError(f"goals.yaml: top-level must be a mapping, got {type(raw).__name__}")

    version = raw.get("version")
    owner = raw.get("owner")
    last_reviewed = raw.get("last_reviewed")
    goals_raw = raw.get("goals", [])
    authority_tiers = raw.get("authority_tiers", {})
    overrides_raw = raw.get("tier_overrides", [])
    impact_raw = raw.get("impact_classification", {})
    contract_raw = raw.get("action_output_contract", {})

    if not isinstance(version, int):
        errors.append(f"version must be int, got {type(version).__name__}")
    if not isinstance(owner, str) or not owner.strip():
        errors.append("owner must be a non-empty string")
    if not isinstance(last_reviewed, str):
        last_reviewed = str(last_reviewed) if last_reviewed is not None else ""

    if not isinstance(goals_raw, list) or not goals_raw:
        errors.append("goals must be a non-empty list")

    goals: list[Goal] = []
    seen_ids: set[str] = set()
    for i, g in enumerate(goals_raw):
        if not isinstance(g, dict):
            errors.append(f"goals[{i}]: must be a mapping")
            continue
        gid = g.get("id", "")
        if gid in seen_ids:
            errors.append(f"goals[{i}].id '{gid}' is duplicated")
        seen_ids.add(gid)

        if not isinstance(gid, str) or not gid.strip():
            errors.append(f"goals[{i}].id must be non-empty string")
            continue

        rubric = g.get("scoring_rubric", {})
        if not isinstance(rubric, dict) or not rubric:
            errors.append(f"goals[{i}] '{gid}': scoring_rubric must be a non-empty mapping")
        else:
            for score, desc in rubric.items():
                try:
                    s_int = int(score)
                except (TypeError, ValueError):
                    errors.append(f"goals[{i}] '{gid}': scoring_rubric key {score!r} is not an int")
                    continue
                if not 0 <= s_int <= 3:
                    errors.append(f"goals[{i}] '{gid}': scoring_rubric key {s_int} out of range 0-3")

        default_auth = g.get("default_authority", "")
        if default_auth not in AUTHORITY_TIER_KEYS:
            errors.append(
                f"goals[{i}] '{gid}': default_authority {default_auth!r} "
                f"not in {sorted(AUTHORITY_TIER_KEYS)}"
            )

        constraints_raw = g.get("constraints", {}) or {}
        constraints = GoalConstraints(
            required_evidence=constraints_raw.get("required_evidence"),
            max_effort_per_action_hours=constraints_raw.get("max_effort_per_action_hours"),
            exploration_bonus=constraints_raw.get("exploration_bonus"),
        )
        if constraints.max_effort_per_action_hours is not None and not isinstance(
            constraints.max_effort_per_action_hours, int
        ):
            errors.append(
                f"goals[{i}] '{gid}': max_effort_per_action_hours must be int"
            )

        scope_raw = g.get("scope", {}) or {}
        goals.append(
            Goal(
                id=gid,
                name=g.get("name", gid),
                priority=int(g.get("priority", 99)),
                description=g.get("description", ""),
                scoring_rubric={int(k): v for k, v in rubric.items()} if isinstance(rubric, dict) else {},
                constraints=constraints,
                default_authority=default_auth,
                applies_to=list(scope_raw.get("applies_to", []) or []),
                excludes=list(scope_raw.get("excludes", []) or []),
            )
        )

    goals.sort(key=lambda g: g.priority)

    # Authority tiers
    pre_check_block: list[str] = []
    for tier_key, tier_def in authority_tiers.items():
        if not isinstance(tier_def, dict):
            errors.append(f"authority_tiers.{tier_key}: must be a mapping")
            continue
        if tier_def.get("pre_check_block"):
            pre_check_block = list(tier_def["pre_check_block"])

    # Output contract
    contract_required = []
    contract_rule_ids = []
    if contract_raw:
        contract_required = list(contract_raw.get("required_fields", []) or [])
        contract_rule_ids = [
            r.get("rule_id", "")
            for r in contract_raw.get("rejection_rules", []) or []
            if isinstance(r, dict)
        ]
        if not contract_required:
            errors.append("action_output_contract.required_fields missing")

    # Atom vocab (declared in yaml, must match local constants)
    declared_atom_types = set(raw.get("atom_types", []) or [])
    declared_atom_evidence = set(raw.get("atom_evidence", []) or [])
    if declared_atom_types != ATOM_TYPES:
        errors.append(
            f"atom_types mismatch with src/goals.py constants: "
            f"yaml={sorted(declared_atom_types)} code={sorted(ATOM_TYPES)}"
        )
    if declared_atom_evidence != ATOM_EVIDENCE:
        errors.append(
            f"atom_evidence mismatch with src/goals.py constants: "
            f"yaml={sorted(declared_atom_evidence)} code={sorted(ATOM_EVIDENCE)}"
        )

    # Tier overrides
    if not isinstance(overrides_raw, list):
        errors.append("tier_overrides must be a list")
    else:
        for j, ov in enumerate(overrides_raw):
            if not isinstance(ov, dict) or "match" not in ov or "force_tier" not in ov:
                errors.append(f"tier_overrides[{j}] must have 'match' and 'force_tier'")
                continue
            if ov["force_tier"] not in AUTHORITY_TIER_KEYS:
                errors.append(
                    f"tier_overrides[{j}].force_tier {ov['force_tier']!r} invalid"
                )

    if errors:
        msg = "goals.yaml validation failed:\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)

    return GoalsConfig(
        version=version,
        owner=owner,
        last_reviewed=last_reviewed,
        goals=goals,
        authority_tier_keys=sorted(AUTHORITY_TIER_KEYS, key=lambda k: TIER_ORDER[k]),
        pre_check_block=pre_check_block,
        output_contract=ActionOutputContract(
            required_fields=contract_required,
            rejection_rule_ids=contract_rule_ids,
        ),
        atom_types=sorted(ATOM_TYPES),
        atom_evidence=sorted(ATOM_EVIDENCE),
        tier_overrides=overrides_raw,
        impact_classification=impact_raw,
    )


def get_goal(cfg: GoalsConfig, goal_id: str) -> Optional[Goal]:
    for g in cfg.goals:
        if g.id == goal_id:
            return g
    return None
