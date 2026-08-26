from __future__ import annotations

import json
from typing import Optional

from src import db
from src.config import get_channels_file


def load_channels_file() -> dict:
    path = get_channels_file()
    if not path.exists():
        return {"channels": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"channels": []}


def save_channels_file(data: dict) -> None:
    path = get_channels_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_channel(channel_url: str, channel_id: Optional[str] = None, channel_name: Optional[str] = None) -> dict:
    from src.feeds import resolve_channel_id

    resolved_id, canonical_url, name = resolve_channel_id(channel_url)
    if not resolved_id and not channel_id:
        raise ValueError(f"Could not resolve channel ID from: {channel_url}")

    final_id = channel_id or resolved_id
    final_name = channel_name or name

    db.upsert_channel(
        channel_id=final_id,
        channel_url=canonical_url,
        channel_name=final_name,
    )

    data = load_channels_file()
    existing_ids = {c["channel_id"] for c in data["channels"]}
    if final_id not in existing_ids:
        data["channels"].append({
            "channel_id": final_id,
            "channel_url": canonical_url,
            "channel_name": final_name,
        })
        save_channels_file(data)

    return {"channel_id": final_id, "channel_url": canonical_url, "channel_name": final_name}


def remove_channel(channel_id_or_url: str) -> bool:
    from src.feeds import resolve_channel_id

    if channel_id_or_url.startswith('http') or channel_id_or_url.startswith('@'):
        resolved_id, _, _ = resolve_channel_id(channel_id_or_url)
        if resolved_id:
            channel_id_or_url = resolved_id

    removed = db.remove_channel(channel_id_or_url)

    data = load_channels_file()
    new_list = [c for c in data["channels"] if c["channel_id"] != channel_id_or_url]
    if len(new_list) != len(data["channels"]):
        save_channels_file({"channels": new_list})
        return True
    return removed


def list_channels() -> list[dict]:
    return db.list_channels()