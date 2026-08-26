from pathlib import Path
from typing import Optional


PROMPTS_DIR = Path(__file__).parent


def list_versions() -> list[str]:
    return sorted(
        d.name for d in PROMPTS_DIR.iterdir() if d.is_dir() and not d.name.startswith('_')
    )


def load_prompt(version: str, name: str) -> str:
    path = PROMPTS_DIR / version / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def resolve_version(requested: Optional[str]) -> str:
    if requested:
        if not (PROMPTS_DIR / requested).is_dir():
            raise FileNotFoundError(f"Unknown prompt version: {requested}")
        return requested
    versions = list_versions()
    if not versions:
        raise FileNotFoundError(f"No prompt versions found in {PROMPTS_DIR}")
    return versions[-1]