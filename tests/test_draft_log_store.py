from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_log_store import (
    map_discord_to_draftmancer,
    post_team_logs,
    render_pool,
)


def _log():
    return {
        "carddata": {
            "c1": {"name": "Lightning Bolt"},
            "c2": {"name": "Counterspell"},
            "c3": {"name": "Fable of the Mirror-Breaker"},  # DFC front only
        },
        "users": {
            "u1": {"userName": "Alice", "cards": ["c1", "c1", "c2", "c3"]},
            "u2": {"userName": "Bob", "cards": []},
        },
    }


def test_render_pool_aggregates_counts_and_uses_names():
    out = render_pool(_log(), "u1")
    lines = out.splitlines()
    assert "2 Lightning Bolt" in lines
    assert "1 Counterspell" in lines
    assert "1 Fable of the Mirror-Breaker" in lines
    assert len(lines) == 3


def test_render_pool_empty_for_unknown_or_cardless_user():
    assert render_pool(_log(), "u2") == ""
    assert render_pool(_log(), "nope") == ""


def _team_log():
    return {
        "carddata": {"c1": {"name": "Lightning Bolt"}, "c2": {"name": "Counterspell"}},
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0, "cards": ["c1"]},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": ["c2"]},
        },
    }


def test_map_discord_to_draftmancer_by_seat_order():
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob"}   # insertion order == seat order
    mapping = map_discord_to_draftmancer(_team_log(), sign_ups)
    assert mapping == {"disc_a": "dm_a", "disc_b": "dm_b"}


def _channel(name):
    ch = MagicMock()
    ch.name = name
    ch.send = AsyncMock()
    return ch


@pytest.mark.asyncio
async def test_post_team_logs_scopes_pools_to_own_team_and_stamps():
    ds = SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42",
        draft_data=_team_log(), team_logs_posted_at=None,
        team_a=["disc_a"], team_b=["disc_b"],
        sign_ups={"disc_a": "Alice", "disc_b": "Bob"},
        channel_ids=[111, 222],
    )
    # db_session yields ds twice (read, then stamp)
    session = MagicMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = ds
    session.execute = AsyncMock(return_value=result); session.commit = AsyncMock()
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)

    red = _channel("Red-Team-Chat-ABC")
    blue = _channel("Blue-Team-Chat-ABC")
    guild = MagicMock()
    guild.get_channel = lambda cid: {111: red, 222: blue}.get(cid)
    bot = MagicMock(); bot.get_guild.return_value = guild

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)):
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    # Red got Alice's pool only; Blue got Bob's pool only
    red_names = [c.kwargs["file"][1] for c in red.send.await_args_list]
    blue_names = [c.kwargs["file"][1] for c in blue.send.await_args_list]
    assert red_names == ["Alice.txt"]
    assert blue_names == ["Bob.txt"]


@pytest.mark.asyncio
async def test_post_team_logs_no_channels_found_does_not_stamp():
    ds = SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42",
        draft_data=_team_log(), team_logs_posted_at=None,
        team_a=["disc_a"], team_b=["disc_b"],
        sign_ups={"disc_a": "Alice", "disc_b": "Bob"},
        channel_ids=[111, 222],
    )
    session = MagicMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = ds
    session.execute = AsyncMock(return_value=result); session.commit = AsyncMock()
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)

    guild = MagicMock()
    guild.get_channel = lambda cid: None  # neither Red nor Blue channel resolves
    bot = MagicMock(); bot.get_guild.return_value = guild

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)):
        ok = await post_team_logs("sid", bot)

    assert ok is False
    assert ds.team_logs_posted_at is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_team_logs_idempotent_when_already_posted():
    from datetime import datetime
    ds = SimpleNamespace(session_id="sid", team_logs_posted_at=datetime.now(),
                         draft_data=_team_log())
    session = MagicMock()
    result = MagicMock(); result.scalar_one_or_none.return_value = ds
    session.execute = AsyncMock(return_value=result); session.commit = AsyncMock()
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)
    bot = MagicMock()
    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)):
        ok = await post_team_logs("sid", bot)
    assert ok is True
    bot.get_guild.assert_not_called()
