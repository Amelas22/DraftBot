"""Donation day in Discord: a receipt per donation, and a live leaderboard message.

Both are driven by the publisher tick from the same ledger credits as the
public page (services/donation_day), so every way money reaches DraftBot is
announced -- auto-draw, /wallet pay, cash self-marks, direct gifts -- without
a hook in any money path. Credits carry a stable key; the keys already
announced are kept in the guild config so a restart never repeats a receipt.
"""
from datetime import datetime
from typing import Any, TypedDict

import discord

from config import get_config, update_setting
from services.donation_day import Credit, display_name

MAX_RECEIPTS_PER_TICK = 10
LEADERBOARD_ROWS = 15
CONFIG_KEY = "donation_day_discord"
FIELD_LIMIT = 1024      # Discord's cap on an embed field value


class DiscordState(TypedDict):
    receipts_channel_id: str | None
    leaderboard_channel_id: str | None
    leaderboard_message_id: str | None
    announced: list[str]


def get_state(guild_id: str | int) -> DiscordState | None:
    raw: Any = get_config(guild_id).get(CONFIG_KEY)
    if not isinstance(raw, dict):
        return None
    return DiscordState(
        receipts_channel_id=_opt_str(raw.get("receipts_channel_id")),
        leaderboard_channel_id=_opt_str(raw.get("leaderboard_channel_id")),
        leaderboard_message_id=_opt_str(raw.get("leaderboard_message_id")),
        announced=[str(k) for k in raw.get("announced") or []])


def save_state(guild_id: str | int, state: DiscordState) -> None:
    update_setting(guild_id, CONFIG_KEY, dict(state))


def _opt_str(value: Any) -> str | None:
    return str(value) if value else None


def _credit_time(credit: Credit) -> datetime:
    return credit.at


def _message_time(message: tuple[datetime, list[str], str]) -> datetime:
    return message[0]


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " & " + names[-1]


def receipt_messages(credits: list[Credit], names: dict[str, str], anonymous: set[str],
                     announced: set[str]) -> list[tuple[list[str], str]]:
    """The receipts still to post, oldest first: (credit keys covered, text).

    A draft's win credits share one receipt naming every winner not yet
    announced; each payment or gift is its own receipt.
    """
    fresh = sorted((c for c in credits if c.key and c.key not in announced), key=_credit_time)
    messages: list[tuple[datetime, list[str], str]] = []
    wins: dict[str, list[Credit]] = {}
    for credit in fresh:
        if credit.kind == "won":
            wins.setdefault(credit.draft, []).append(credit)
            continue
        who = display_name(credit.player_id, names, anonymous)
        if credit.method == "gift":
            text = f"💸 **{who}** donated {credit.amount} tix to DraftBot"
        else:
            label = " (cash)" if credit.method == "cash" else ""
            text = f"💵 **{who}** paid {credit.amount} tix to DraftBot{label}"
        messages.append((credit.at, [credit.key], text))
    for draft, group in wins.items():
        winners = _join_names([display_name(c.player_id, names, anonymous) for c in group])
        total = sum(c.amount for c in group)
        messages.append((group[0].at, [c.key for c in group],
                         f"🏆 **{winners}** won {total} tix for DraftBot (draft {draft})"))
    messages.sort(key=_message_time)
    return [(keys, text) for _, keys, text in messages]


def leaderboard_embed(payload: dict[str, Any], page_url: str | None) -> discord.Embed:
    """The leaderboard message: totals, then the top rows by total."""
    description = (f"**${payload.get('raised', 0)}** raised of ${payload.get('goal', 0)} · "
                   f"${payload.get('pledged', 0)} pledged")
    if page_url:
        description += f"\n{page_url}"
    embed = discord.Embed(title=f"🏆 {payload.get('name', 'Donation Day')}",
                          description=description, color=discord.Color.purple())
    rows = list(payload.get("rows") or [])[:LEADERBOARD_ROWS]
    lines = [f"**{i}. {r['name']}** — {r['total']} (won {r['won']} · paid {r['paid']})"
             for i, r in enumerate(rows, start=1)]
    value = "\n".join(lines) or "No donations yet."
    embed.add_field(name="Leaderboard", value=value[:FIELD_LIMIT], inline=False)
    embed.set_footer(text=f"1 tix = $1 · updated {str(payload.get('updated_at', '')).replace('T', ' ')}")
    return embed
