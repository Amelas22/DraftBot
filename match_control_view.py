"""Discord glue for a tournament match's control message.

Top-level module (like tournament_nudge.py) so the tournament cog, the draft
cog, the premade session hook, utils.py and views.py can all import it without
an import cycle through cogs/. The message body itself is rendered by
helpers/match_control.py, which stays free of Discord imports.
"""
from typing import Any

import discord
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from helpers.match_control import SCHEDULING, match_state, render_match_control
from models.draft_session import DraftSession
from models.tournament import TournamentMatch, TournamentParticipant, TournamentRound


async def match_facts(
    session: AsyncSession, match_id: int
) -> tuple[TournamentMatch, str, str, int, DraftSession | None] | None:
    """(match, a_name, b_name, round_number, draft) for a match, or None.

    ``draft`` is the DraftSession linked to this match, or None. One query set
    feeding every render, so the state can never be derived from a half-stale
    picture.
    """
    row = (await session.execute(
        select(TournamentMatch, TournamentRound.round_number)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentMatch.id == match_id)
    )).first()
    if row is None:
        return None
    match, round_number = row
    part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
    part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
    if part_a is None or part_b is None:
        return None
    draft = (await session.execute(
        select(DraftSession).where(DraftSession.tournament_match_id == match_id)
    )).scalars().first()
    return match, part_a.team_name, part_b.team_name, round_number, draft


def lobby_link(draft: DraftSession | None) -> str | None:
    """Jump URL for a linked draft's lobby message, or None."""
    if draft is None or not draft.message_id or not draft.draft_channel_id:
        return None
    return (f"https://discord.com/channels/{draft.guild_id}/"
            f"{draft.draft_channel_id}/{draft.message_id}")


def control_body_and_view(
    match: TournamentMatch, a_name: str, b_name: str, round_number: int, draft: DraftSession | None
) -> tuple[str, Any]:
    """(body, view) for a match's control message. View is None off `scheduling`."""
    state = match_state(match.team_a_wins is not None, draft is not None)
    body = render_match_control(
        state, a_name, b_name, round_number,
        lobby_link=lobby_link(draft),
        result=(match.team_a_wins, match.team_b_wins),
    )
    return body, (MatchControlView(match.id) if state == SCHEDULING else None)


class MatchControlView(discord.ui.View):
    """Persistent 'Start draft' button — filled in by Task 4."""

    def __init__(self, match_id: int) -> None:
        super().__init__(timeout=None)
        self.match_id = match_id
