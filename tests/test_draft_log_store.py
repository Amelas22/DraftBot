import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.draft_log_store import (
    _post_pools_for_team,
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


def _team3_log():
    return {
        "carddata": {
            "c1": {"name": "Lightning Bolt"},
            "c2": {"name": "Counterspell"},
            "c3": {"name": "Giant Growth"},
        },
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0, "cards": ["c1"]},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": ["c2"]},
            "dm_c": {"userName": "Carol", "seatNum": 2, "cards": ["c3"]},
        },
    }


def _channel(name):
    ch = MagicMock()
    ch.name = name
    ch.send = AsyncMock()
    return ch


def _summary_channel(name, thread=None, create_thread_error=None):
    """Channel stand-in for the resolve-or-create step: `channel.send()`
    resolves to a summary message whose `create_thread()` either resolves to
    `thread` or raises `create_thread_error` (simulating Discord refusing
    thread creation, e.g. missing Manage Threads). The same `channel.send`
    also serves the HTTPException fallback path's per-player posts -- its
    return value is unused there, so one AsyncMock covers both."""
    ch = _channel(name)
    summary = MagicMock()
    if create_thread_error is not None:
        summary.create_thread = AsyncMock(side_effect=create_thread_error)
    else:
        summary.create_thread = AsyncMock(return_value=thread)
    ch.send = AsyncMock(return_value=summary)
    ch.summary = summary
    return ch


class _FakeThread:
    """A thread stand-in real enough for _post_pools_for_team's resume logic:
    .send() records each message's attachment filenames, and .history()
    replays them back -- so a second call against the same instance really
    discovers what the first call posted, instead of the test hand-authoring
    the filename set."""

    def __init__(self, tid):
        self.id = tid
        self._messages = []
        self.send = AsyncMock(side_effect=self._do_send)

    async def _do_send(self, content=None, files=None):
        msg = MagicMock()
        msg.attachments = [
            MagicMock(filename=(f[1] if isinstance(f, tuple) else getattr(f, "filename", None)))
            for f in (files or [])
        ]
        self._messages.append(msg)
        return msg

    async def history(self, limit=None):
        for msg in self._messages:
            yield msg

    def posted_txt_filenames(self):
        """Filenames of .txt attachments actually recorded as sent -- unlike
        _attachment_names (which reads Mock.await_args_list and so includes
        calls whose side_effect raised), this only counts sends that really
        went through."""
        return [
            a.filename for msg in self._messages for a in msg.attachments
            if a.filename.endswith(".txt")
        ]


class _FlakyThread(_FakeThread):
    """Fails Bob's very first send (simulating a transient error) but
    succeeds on every other send, including a retry."""

    def __init__(self, tid):
        super().__init__(tid)
        self._bob_attempts = 0

    async def _do_send(self, content=None, files=None):
        if content and "Bob" in content and self._bob_attempts == 0:
            self._bob_attempts += 1
            raise RuntimeError("discord hiccup")
        return await super()._do_send(content=content, files=files)


def _team_ds(team_b=("disc_b",), channel_ids=(111, 222), friendly_id="ABC"):
    """DraftSession stand-in for post_team_logs: Alice (disc_a) on team A, Bob
    (disc_b) on team B, unposted, with the Red/Blue channel ids. sign_ups always
    holds both players so the sign-up count matches _team_log()'s two non-bot
    users; single-team tests override team_b/channel_ids."""
    return SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42", friendly_id=friendly_id,
        draft_data=_team_log(), team_logs_posted_at=None,
        team_a=["disc_a"], team_b=list(team_b),
        sign_ups={"disc_a": "Alice", "disc_b": "Bob"},
        channel_ids=list(channel_ids),
        team_a_pools_thread_id=None, team_b_pools_thread_id=None,
    )


def _team_ds3(friendly_id="ABC"):
    """DraftSession stand-in with a single 3-player team A and an empty team B."""
    return SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42", friendly_id=friendly_id,
        draft_data=_team3_log(), team_logs_posted_at=None,
        team_a=["disc_a", "disc_b", "disc_c"], team_b=[],
        sign_ups={"disc_a": "Alice", "disc_b": "Bob", "disc_c": "Carol"},
        channel_ids=[111],
        team_a_pools_thread_id=None, team_b_pools_thread_id=None,
    )


def _db_ctx(ds):
    """`(session, ctx)` standing in for `async with db_session() as session`,
    where every query resolves to `ds` (post_team_logs reads it, persists
    thread ids into it, then re-reads it to stamp -- all the same object).
    Returns `session` too so tests can assert on commit."""
    result = MagicMock(); result.scalar_one_or_none.return_value = ds
    session = MagicMock()
    session.execute = AsyncMock(return_value=result); session.commit = AsyncMock()
    ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=session); ctx.__aexit__ = AsyncMock(return_value=None)
    return session, ctx


def _bot_for(channels):
    """Bot whose guild resolves exactly the given `{id: channel_or_thread}` map
    (via guild.get_channel, used to find team channels) and whose own
    get_channel resolves the same map (used to resolve a stored thread id);
    any other id resolves to None. fetch_channel -- the restart-safe fallback
    when get_channel misses -- raises HTTPException by default; tests that
    need it to succeed register the thread in `channels` instead."""
    guild = MagicMock()
    guild.get_channel = lambda cid: channels.get(cid)
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.get_channel = lambda cid: channels.get(cid)
    bot.fetch_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "not found"))
    return bot


def _attachment_names(destination):
    """Filenames of every attachment across all `destination.send` calls, in
    order. These tests patch discord.File to a ("FILE", filename) tuple. The
    one-off summary send carries no `files` kwarg at all."""
    return [
        f[1] for call in destination.send.await_args_list for f in call.kwargs.get("files", [])
    ]


async def _noop_persist(_thread_id: str) -> None:
    pass


@pytest.mark.asyncio
async def test_post_team_logs_scopes_pools_to_own_team_and_stamps():
    ds = _team_ds()
    _, ctx = _db_ctx(ds)
    red_thread, blue_thread = _FakeThread(1001), _FakeThread(2002)
    red = _summary_channel("Red-Team-Chat-ABC", thread=red_thread)
    blue = _summary_channel("Blue-Team-Chat-ABC", thread=blue_thread)
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)   # best-effort: no image → txt only
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    # Thread ids persisted onto the session for a retry to find.
    assert ds.team_a_pools_thread_id == "1001"
    assert ds.team_b_pools_thread_id == "2002"
    # One summary message per team channel; the pools themselves land in the thread.
    assert red.send.await_count == 1
    assert blue.send.await_count == 1
    assert _attachment_names(red_thread) == ["Alice.txt"]
    assert _attachment_names(blue_thread) == ["Bob.txt"]


@pytest.mark.asyncio
async def test_post_team_logs_matches_lowercased_discord_channel_names():
    """Discord stores text-channel names lowercased in production, e.g.
    'red-team-chat-abc', not 'Red-Team-Chat-ABC'. Channel matching must be
    case-insensitive or post_team_logs silently posts nothing in prod."""
    ds = _team_ds()
    _, ctx = _db_ctx(ds)
    red_thread, blue_thread = _FakeThread(1001), _FakeThread(2002)
    red = _summary_channel("red-team-chat-abc", thread=red_thread)
    blue = _summary_channel("blue-team-chat-abc", thread=blue_thread)
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    assert _attachment_names(red_thread) == ["Alice.txt"]
    assert _attachment_names(blue_thread) == ["Bob.txt"]


@pytest.mark.asyncio
async def test_post_team_logs_attaches_deck_image_alongside_txt():
    """When a pile image builds, the member's post carries BOTH the .txt and a
    .jpg deck image in one message."""
    ds = _team_ds(team_b=(), channel_ids=(111,))
    _, ctx = _db_ctx(ds)
    thread = _FakeThread(1001)
    red = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=io.BytesIO(b"\xff\xd8jpg"))
        ok = await post_team_logs("sid", bot)

    assert ok is True
    # Both attachments, one message, inside the thread.
    assert _attachment_names(thread) == ["Alice.txt", "Alice.jpg"]


@pytest.mark.asyncio
async def test_post_team_logs_still_posts_txt_when_image_build_raises():
    """Best-effort: an image build failure must NOT block the .txt post or the
    stamp (the .txt is the deliverable; post_team_logs is reconciler-driven)."""
    ds = _team_ds(team_b=(), channel_ids=(111,))
    _, ctx = _db_ctx(ds)
    thread = _FakeThread(1001)
    red = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(side_effect=RuntimeError("scryfall down"))
        ok = await post_team_logs("sid", bot)

    assert ok is True                                       # still succeeded
    assert ds.team_logs_posted_at is not None               # still stamped
    assert _attachment_names(thread) == ["Alice.txt"]        # txt only, image skipped


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
    assert ds.team_a_pools_thread_id is None
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
    ds = _team_ds()
    _, ctx = _db_ctx(ds)

    red_thread, blue_thread = _FakeThread(1001), _FakeThread(2002)
    red, blue = _channel("Red-Team-Chat-ABC"), _channel("Blue-Team-Chat-ABC")

    def _slow_summary(thread):
        summary = MagicMock()
        summary.create_thread = AsyncMock(return_value=thread)

        async def _send(*a, **k):
            await asyncio.sleep(0.05)   # keep run 1 in flight while run 2 starts
            return summary
        return AsyncMock(side_effect=_send)

    red.send = _slow_summary(red_thread)
    blue.send = _slow_summary(blue_thread)
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
    assert red.send.await_count == 1                 # one summary message, not two
    assert blue.send.await_count == 1
    assert red_thread.send.await_count == 1           # Alice posted exactly once
    assert blue_thread.send.await_count == 1          # Bob posted exactly once


@pytest.mark.asyncio
async def test_post_team_logs_three_player_team_gets_one_summary_and_three_thread_posts():
    ds = _team_ds3()
    _, ctx = _db_ctx(ds)
    thread = _FakeThread(9001)
    red = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_a_pools_thread_id == "9001"
    assert red.send.await_count == 1
    assert red.send.await_args.kwargs["content"] == "📥 **Drafted pools** — ABC (3 players)"
    red.summary.create_thread.assert_awaited_once()
    assert red.summary.create_thread.await_args.kwargs["name"] == "Drafted pools — ABC"
    assert red.summary.create_thread.await_args.kwargs["auto_archive_duration"] == 10080
    assert _attachment_names(thread) == ["Alice.txt", "Bob.txt", "Carol.txt"]


@pytest.mark.asyncio
async def test_post_pools_for_team_resumes_into_stored_thread_and_reposts_nothing_when_all_present():
    """The thread id is persisted, and a second call with that id posts
    nothing more once every player is already present."""
    draft_data = _team3_log()
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob", "disc_c": "Carol"}
    mapping = {"disc_a": "dm_a", "disc_b": "dm_b", "disc_c": "dm_c"}
    thread = _FakeThread(9001)
    channel = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({})
    persisted = []

    async def persist(tid):
        persisted.append(tid)

    with patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        first_id, first_ok = await _post_pools_for_team(
            bot, channel, None, "ABC", ["disc_a", "disc_b", "disc_c"], mapping, draft_data, sign_ups, persist,
        )
        assert first_ok is True
        assert first_id == "9001"

        channel.send.reset_mock()   # only care whether run 2 opens a NEW summary/thread
        bot2 = _bot_for({9001: thread})   # second run resolves the stored id via bot.get_channel

        second_id, second_ok = await _post_pools_for_team(
            bot2, channel, first_id, "ABC", ["disc_a", "disc_b", "disc_c"], mapping, draft_data, sign_ups, persist,
        )

    assert second_id == first_id == "9001"
    assert second_ok is True
    channel.send.assert_not_awaited()          # no second summary message / no second thread
    assert persisted == ["9001"]               # persisted exactly once, at creation
    assert _attachment_names(thread) == ["Alice.txt", "Bob.txt", "Carol.txt"]   # unchanged


@pytest.mark.asyncio
async def test_post_team_logs_one_players_send_failure_leaves_stamp_unset_then_retries_into_same_thread():
    """A run where one player's send raises: the other players still post,
    the stamp is NOT set, and a follow-up run posts only the missing player
    into the SAME thread (no second thread)."""
    ds = _team_ds3()
    _, ctx = _db_ctx(ds)
    thread = _FlakyThread(9001)
    red = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({111: red})   # thread not registered yet -- created fresh on the first run

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        first_ok = await post_team_logs("sid", bot)

    assert first_ok is False
    assert ds.team_logs_posted_at is None
    assert ds.team_a_pools_thread_id == "9001"                          # persisted despite the failure
    assert thread.posted_txt_filenames() == ["Alice.txt", "Carol.txt"]  # Bob missing

    # Retry: the stored thread id must resolve (bot.get_channel) so the run
    # resumes into it rather than opening a second one; Bob's earlier hiccup
    # doesn't recur.
    bot2 = _bot_for({111: red, 9001: thread})
    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), \
         patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        second_ok = await post_team_logs("sid", bot2)

    assert second_ok is True
    assert ds.team_logs_posted_at is not None
    assert ds.team_a_pools_thread_id == "9001"                # no second thread created
    assert red.send.await_count == 1                          # one summary across both runs
    assert thread.posted_txt_filenames() == ["Alice.txt", "Carol.txt", "Bob.txt"]   # only Bob added


@pytest.mark.asyncio
async def test_post_pools_for_team_falls_back_to_channel_posts_when_thread_creation_refused():
    """If thread creation raises discord.HTTPException (typically missing
    Manage Threads), fall back to posting the per-player messages directly
    into the channel exactly as before threading existed."""
    draft_data = _team_log()
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob"}
    mapping = {"disc_a": "dm_a", "disc_b": "dm_b"}
    channel = _summary_channel(
        "Red-Team-Chat-ABC",
        create_thread_error=discord.HTTPException(MagicMock(), "no Manage Threads"),
    )
    bot = _bot_for({})
    persisted = []

    async def persist(tid):
        persisted.append(tid)

    with patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = AsyncMock(return_value=None)
        thread_id, all_posted = await _post_pools_for_team(
            bot, channel, None, "ABC", ["disc_a"], mapping, draft_data, sign_ups, persist,
        )

    assert thread_id is None
    assert all_posted is True
    assert persisted == []                       # no thread was ever created
    channel.summary.create_thread.assert_awaited_once()
    # Both the summary attempt and the per-player fallback post went straight
    # to the channel: two sends total, the second carrying Alice's pool.
    assert channel.send.await_count == 2
    assert _attachment_names(channel) == ["Alice.txt"]
