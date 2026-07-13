"""Shared draft-log helpers that are new to the reconciliation design:
pool rendering and per-team pool posting. Capture/publish mechanics remain on
DraftSetupManager (the single home); this module holds only the genuinely-new
logic so it can be unit-tested in isolation."""
from __future__ import annotations


def render_pool(draft_data: dict, user_id: str) -> str:
    """Importable decklist for one drafter's full pool: `"<count> <CardName>"`
    lines from `users[user_id].cards`, using the front-face card name. Returns
    "" if the user or their cards are missing."""
    users = draft_data.get("users") or {}
    user = users.get(user_id) or {}
    carddata = draft_data.get("carddata") or {}
    card_ids = user.get("cards") or []

    counts: dict[str, int] = {}
    order: list[str] = []
    for cid in card_ids:
        name = (carddata.get(cid) or {}).get("name")
        if not name:
            continue
        if name not in counts:
            order.append(name)
        counts[name] = counts.get(name, 0) + 1
    return "\n".join(f"{counts[name]} {name}" for name in order)
