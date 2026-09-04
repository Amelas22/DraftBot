"""One self-contained draft table page from a stored Draftmancer log.

Replays a whole table rather than one seat: a global clock, all boosters in
flight at once, any pack pinnable to follow it round the ring, and every card's
fate one click away.

Card art is loaded from Scryfall by URL, so the page must be hosted -- it will
not render art from a file:// copy. Everything else is inline, because a hosted
page has no second request to fetch it from.
"""
import json
import re
from html import escape
from pathlib import Path
from typing import Any

from helpers import scryfall_oracle
from helpers.team_names import team_labels
from services.draft_log_store import map_discord_to_draftmancer
from services.draft_reconstruct import (
    card_fates, reconstruct, seat_pools, taken_in,
)

ASSETS = Path(__file__).resolve().parent / "draft_table_assets"
EMOJI_TOKEN = re.compile(r"<a?:\w+:\d+>")


def display_name(name: Any) -> str:
    """Seat name without Discord custom-emoji tokens, which render as raw text."""
    cleaned = re.sub(r"\s+", " ", EMOJI_TOKEN.sub("", str(name))).strip()
    return cleaned or str(name)


def _sign_up_name(value: Any) -> Any:
    """A sign_ups entry's display name.

    Usually a bare str, but generate_magicprotools_embed (draft_setup_manager.py)
    shows some rows store {"name": ...} instead -- match its handling so a dict
    row doesn't render as its own str(dict) here.
    """
    if isinstance(value, dict):
        return value.get("name", value)
    return value


def _seat_names(draft_session: Any, draft_data: dict[str, Any]) -> dict[str, str]:
    """{seat name as the log spells it: "A" | "B"} for every signed-up player.

    The viewer looks a seat up by the name the Draftmancer log gives it, so this
    map has to be keyed the same way. Prefer the positional alignment
    `map_discord_to_draftmancer` already does for pool posting -- a player who
    renames in the Draftmancer client keeps their seat number but not their
    name, and a seat that misses this map is not merely unlabelled: the viewer
    falls back to side A, i.e. shows them on the wrong team.

    Falls back to matching on the stored sign-up name when the positional
    alignment refuses (it returns {} when the player count doesn't match, rather
    than risk mis-assigning a seat). That is the weaker join, but it is what
    this map used to do, so the fallback can only match more seats than before.
    """
    users = draft_data.get("users") or {}
    by_discord = map_discord_to_draftmancer(draft_data, draft_session.sign_ups or {})
    sign_ups: dict[str, Any] = draft_session.sign_ups or {}

    teams: dict[str, str] = {}
    for members, side in ((draft_session.team_a or [], "A"),
                          (draft_session.team_b or [], "B")):
        for discord_id in members:
            seat = (users.get(by_discord.get(discord_id) or "") or {}).get("userName")
            if not seat and discord_id in sign_ups:
                seat = _sign_up_name(sign_ups[discord_id])
            if seat:
                teams[display_name(seat)] = side
    return teams


def session_meta_from(draft_session: Any, draft_data: dict[str, Any]) -> dict[str, Any]:
    """Header metadata: teams, cube, date, friendly id.

    Takes the row the caller already holds rather than re-querying -- the
    publisher is called from inside publish_draft_log, which loaded it.
    """
    teams = _seat_names(draft_session, draft_data)
    started = draft_session.draft_start_time
    team_a, team_b = team_labels(draft_session.team_a_name, draft_session.team_b_name)
    return {
        "friendlyId": draft_session.friendly_id or draft_session.session_id,
        "cube": draft_session.cube or "",
        "started": str(started)[:16] if started else "",
        "type": draft_session.session_type or "",
        "teamNames": {
            "A": display_name(team_a.name),
            "B": display_name(team_b.name),
        },
        "teams": teams,
    }


def _image(card: dict[str, Any], size: str) -> str:
    """A Scryfall URL at the requested size.

    carddata stores one border_crop URL per language; every other size lives at
    the same path with the size segment swapped, so no extra lookup is needed.
    """
    uris: dict[str, str] = card.get("image_uris") or {}
    url = uris.get("en") or next(iter(uris.values()), "")
    return url.replace("/border_crop/", f"/{size}/") if url else ""


async def build_payload(draft: dict[str, Any], session_meta: dict[str, Any]) -> dict[str, Any]:
    """Everything the page needs, as one JSON-serialisable dict."""
    result = reconstruct(draft)
    fates = card_fates(result)

    carddata: dict[str, Any] = draft.get("carddata") or {}
    # Draftmancer gives no oracle text, no P/T and only a bare type; Scryfall
    # fills those in, keyed by the card id already in the log.
    oracle = await scryfall_oracle.enrich(carddata.keys())

    cards: dict[str, Any] = {}
    for card_id, card in carddata.items():
        cards[card_id] = {
            "name": card.get("name") or card_id,
            "cost": card.get("mana_cost") or "",
            "cmc": card.get("cmc") or 0,
            "colors": card.get("colors") or [],
            "type": card.get("type") or "",
            "rarity": card.get("rarity") or "",
            "small": _image(card, "small"),
            "normal": _image(card, "normal"),
            # one entry per face, so a DFC keeps both halves
            "faces": (oracle.get(card_id) or {}).get("faces") or [],
        }

    taken = taken_in(result)
    boosters = [{
        "index": b.index, "pack": b.pack, "opener": display_name(b.opener),
        "takenAt": {card: {"pick": at["pick"], "seat": display_name(at["seat"])}
                    for card, at in taken[b.index].items()},
        "steps": [{"pack": s.pack, "pick": s.pick, "seat": display_name(s.seat),
                   "contents": s.contents, "taken": s.taken,
                   "inserted": s.inserted} for s in b.steps],
    } for b in result.boosters]

    pools = {display_name(seat): entries
             for seat, entries in seat_pools(result).items()}

    return {
        "meta": session_meta,
        "ring": [display_name(n) for n in result.ring],
        "pools": pools,
        "directions": result.directions,
        "cards": cards,
        "boosters": boosters,
        "events": [{"pack": e.pack, "pick": e.pick, "seat": display_name(e.seat),
                    "description": display_name(e.description)} for e in result.events],
        "fates": {name: {"card": f.card_id, "pack": f.pack, "booster": f.booster_index,
                         "firstSeen": f.first_seen_pick, "takenAt": f.taken_at_pick,
                         "takenBy": display_name(f.taken_by), "wheeled": f.wheeled,
                         "passedBy": [display_name(s) for s in f.passed_by]}
                  for name, f in fates.items()},
    }


async def render(draft_data: dict[str, Any], session_meta: dict[str, Any]) -> str:
    """The page. Data is embedded ahead of the script so it is ready on load."""
    payload = await build_payload(draft_data, session_meta)
    title = f"{payload['meta']['friendlyId']} — draft table"
    # No "<" survives into the data block, so neither "</script>" (which would
    # close it early) nor "<!--<script" (which would put it into HTML's
    # script-data-double-escaped state, after which "</script>" stops closing
    # it) can be formed by a card or player name. < is a JSON string
    # escape, so the parsed value is unchanged.
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    css = (ASSETS / "viewer.css").read_text(encoding="utf-8")
    js = (ASSETS / "viewer.js").read_text(encoding="utf-8")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
{css}
</style>
</head>
<body>
<div id="app"></div>
<script>window.DRAFT = {data};</script>
<script>
{js}
</script>
</body>
</html>
"""
