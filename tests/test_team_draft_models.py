from datetime import datetime

import pytest

from database.db_session import db_session
from models.team_draft import TeamDraft


@pytest.mark.asyncio
async def test_a_team_draft_round_trips_its_sides_and_starts_unbooked(test_db):
    async with db_session() as s:
        td = TeamDraft(guild_id="g", channel_id="c", name="A vs B", side_a_roles=["1"],
                       side_a_users=[], side_b_roles=[], side_b_users=["2", "3"],
                       created_by="9", created_utc=datetime(2026, 10, 26))
        s.add(td)
        await s.commit()
        got = await s.get(TeamDraft, td.id)
    assert (got.side_a_roles, got.side_b_users) == (["1"], ["2", "3"])
    assert got.booked_start_utc is None and got.booking_version == 0 and got.draft_session_id is None
