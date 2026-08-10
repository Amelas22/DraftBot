import io
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


def test_map_discord_to_draftmancer_excludes_bot_users():
    log = {
        "carddata": {"c1": {"name": "Lightning Bolt"}, "c2": {"name": "Counterspell"}},
        "users": {
            "dm_bot": {"userName": "DraftBot", "seatNum": 0, "isBot": True, "cards": []},
            "dm_a": {"userName": "Alice", "seatNum": 1, "cards": ["c1"]},
            "dm_b": {"userName": "Bob", "seatNum": 2, "cards": ["c2"]},
        },
    }
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob"}
    mapping = map_discord_to_draftmancer(log, sign_ups)
    assert mapping == {"disc_a": "dm_a", "disc_b": "dm_b"}


def test_map_discord_to_draftmancer_returns_empty_on_count_mismatch():
    log = {
        "carddata": {},
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0, "cards": []},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": []},
            "dm_c": {"userName": "Carol", "seatNum": 2, "cards": []},
        },
    }
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob"}  # 2 sign-ups vs 3 real players
    mapping = map_discord_to_draftmancer(log, sign_ups)
    assert mapping == {}


def _channel(name):
    ch = MagicMock()
    ch.name = name
    ch.send = AsyncMock()
    return ch


def _team_ds(team_b=("disc_b",), channel_ids=(111, 222)):
    """DraftSession stand-in for post_team_logs: Alice (disc_a) on team A, Bob
    (disc_b) on team B, unposted, with the Red/Blue channel ids. sign_ups always
    holds both players so the sign-up count matches _team_log()'s two non-bot
    users; single-team tests override team_b/channel_ids."""
    return SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42",
        draft_data=_team_log(), team_logs_posted_at=None,
        team_a=["disc_a"], team_b=list(team_b),
        sign_ups={"disc_a": "Alice", "disc_b": "Bob"},
        channel_ids=list(channel_ids),
    )


def _db_ctx(ds):
    """`(session, ctx)` standing in for `async with db_session() as session`,
    where every query resolves to `ds` (post_team_logs reads it once, then
    re-reads it to stamp). Returns `session` too so tests can assert on commit."""
    result = MagicMock(); result.scalar_one_or_none.return_value = ds
    session = MagicMock()
    session.execute = AsyncMock(return_value=result); session.commit = AsyncMock()
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)
    return session, ctx


def _bot_for(channels):
    """Bot whose guild resolves exactly the given `{channel_id: channel}` map;
    any other channel id resolves to None."""
    guild = MagicMock()
    guild.get_channel = lambda cid: channels.get(cid)
    bot = MagicMock(); bot.get_guild.return_value = guild
    return bot


def _attachment_names(channel):
    """Filenames of every attachment across all `channel.send` calls, in order.
    These tests patch discord.File to a ("FILE", filename) tuple."""
    return [f[1] for call in channel.send.await_args_list for f in call.kwargs["files"]]


@pytest.mark.asyncio
async def test_post_team_logs_scopes_pools_to_own_team_and_stamps():
    ds = _team_ds()
    _, ctx = _db_ctx(ds)
    red = _channel("Red-Team-Chat-ABC")
    blue = _channel("Blue-Team-Chat-ABC")
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)   # best-effort: no image → txt only
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    # Red got Alice's pool only; Blue got Bob's pool only (as .txt attachments)
    assert _attachment_names(red) == ["Alice.txt"]
    assert _attachment_names(blue) == ["Bob.txt"]


@pytest.mark.asyncio
async def test_post_team_logs_matches_lowercased_discord_channel_names():
    """Discord stores text-channel names lowercased in production, e.g.
    'red-team-chat-abc', not 'Red-Team-Chat-ABC'. Channel matching must be
    case-insensitive or post_team_logs silently posts nothing in prod."""
    ds = _team_ds()
    _, ctx = _db_ctx(ds)
    red = _channel("red-team-chat-abc")
    blue = _channel("blue-team-chat-abc")
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    assert _attachment_names(red) == ["Alice.txt"]
    assert _attachment_names(blue) == ["Bob.txt"]


@pytest.mark.asyncio
async def test_post_team_logs_attaches_deck_image_alongside_txt():
    """When a pile image builds, the member's post carries BOTH the .txt and a
    .jpg deck image in one message."""
    ds = _team_ds(team_b=(), channel_ids=(111,))
    _, ctx = _db_ctx(ds)
    red = _channel("Red-Team-Chat-ABC")
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=io.BytesIO(b"\xff\xd8jpg"))
        ok = await post_team_logs("sid", bot)

    assert ok is True
    # Both attachments, one message.
    assert _attachment_names(red) == ["Alice.txt", "Alice.jpg"]


@pytest.mark.asyncio
async def test_post_team_logs_still_posts_txt_when_image_build_raises():
    """Best-effort: an image build failure must NOT block the .txt post or the
    stamp (the .txt is the deliverable; post_team_logs is reconciler-driven)."""
    ds = _team_ds(team_b=(), channel_ids=(111,))
    _, ctx = _db_ctx(ds)
    red = _channel("Red-Team-Chat-ABC")
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(side_effect=RuntimeError("scryfall down"))
        ok = await post_team_logs("sid", bot)

    assert ok is True                                       # still succeeded
    assert ds.team_logs_posted_at is not None               # still stamped
    assert _attachment_names(red) == ["Alice.txt"]          # txt only, image skipped


@pytest.mark.asyncio
async def test_post_team_logs_partial_channel_resolution_does_not_stamp():
    """All-or-nothing: if only one team's channel resolves, post NOTHING this
    call (not even to the resolved team), don't stamp team_logs_posted_at, and
    return False so the reconciler retries next tick. Posting best-effort to
    the resolved channel would re-post to it (duplicate, player-visible pool
    files) on every retry tick until the other channel resolves."""
    ds = _team_ds()
    session, ctx = _db_ctx(ds)
    red = _channel("Red-Team-Chat-ABC")
    bot = _bot_for({111: red})   # only Red resolves; Blue's channel id has no match

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)):
        ok = await post_team_logs("sid", bot)

    assert ok is False
    assert ds.team_logs_posted_at is None
    red.send.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_team_logs_no_channels_found_does_not_stamp():
    ds = _team_ds()
    session, ctx = _db_ctx(ds)
    bot = _bot_for({})   # neither Red nor Blue channel resolves

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
    _, ctx = _db_ctx(ds)
    bot = MagicMock()
    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)):
        ok = await post_team_logs("sid", bot)
    assert ok is True
    bot.get_guild.assert_not_called()


@pytest.mark.asyncio
async def test_post_team_logs_concurrent_calls_post_once():
    """The endDraft push and a reconciler tick can call while one run is still
    in flight (slow image builds); the overlapping call must be dropped, not
    re-post the pools."""
    import asyncio
    ds = _team_ds()
    _, ctx = _db_ctx(ds)

    red = _channel("Red-Team-Chat-ABC")
    blue = _channel("Blue-Team-Chat-ABC")

    async def slow_send(*a, **k):
        await asyncio.sleep(0.05)   # keep run 1 in flight while run 2 starts
    red.send = AsyncMock(side_effect=slow_send)
    blue.send = AsyncMock(side_effect=slow_send)
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        results = await asyncio.gather(
            post_team_logs("sid", bot),
            post_team_logs("sid", bot),
        )

    assert sorted(results) == [False, True]          # one ran, one was dropped
    assert red.send.await_count == 1                 # Alice posted exactly once
    assert blue.send.await_count == 1                # Bob posted exactly once
