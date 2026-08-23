import asyncio
import contextlib
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


def _collision_log():
    """Two players whose display names sanitise to the same filename: 'Bob!'
    and 'Bob?' both strip down to 'Bob'."""
    return {
        "carddata": {"c1": {"name": "Lightning Bolt"}, "c2": {"name": "Counterspell"}},
        "users": {
            "dm_x": {"userName": "Bob!", "seatNum": 0, "cards": ["c1"]},
            "dm_y": {"userName": "Bob?", "seatNum": 1, "cards": ["c2"]},
        },
    }


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


# The bot's own Discord user id, shared by _bot_for and every fake
# channel/thread below so the "who's already posted" scan (which is scoped to
# messages the bot itself authored) recognizes the bot's own sends.
_BOT_ID = 999000111


# The Alice/Bob pair most of these tests use. None of them is ABOUT the
# sign-ups or the mapping -- it is scaffolding, so it lives here rather than
# three lines at the top of each test.
_SIGN_UPS_AB = {"disc_a": "Alice", "disc_b": "Bob"}
_MAPPING_AB = {"disc_a": "dm_a", "disc_b": "dm_b"}


async def _post_pools(bot, channel, persist, *, destination_id=None,
                      members=("disc_a", "disc_b"), mapping=None, draft_data=None,
                      sign_ups=None, friendly_id="ABC"):
    """Keyword wrapper over _post_pools_for_team's nine positional arguments.

    `mapping` and `sign_ups` are adjacent and both dict[str, str], so a
    transposed pair reads as a plausible call and fails somewhere confusing.
    Naming them at the call site makes that impossible, and adding a
    parameter to the production function no longer means editing 14 tests.
    """
    return await _post_pools_for_team(
        bot, channel, destination_id, friendly_id, list(members),
        mapping if mapping is not None else _MAPPING_AB,
        draft_data if draft_data is not None else _team_log(),
        sign_ups if sign_ups is not None else _SIGN_UPS_AB,
        persist,
    )


def _persist_recorder():
    """`(callback, list)` for _post_pools_for_team's persist_thread_id argument.
    The list records every thread id persisted, in order -- which is the only
    channel production code has for reporting a newly created thread, so it is
    also what a test reads to learn the id."""
    persisted: list[str] = []

    async def persist(thread_id):
        persisted.append(thread_id)

    return persist, persisted


@contextlib.contextmanager
def _patched_discord(build=None):
    """Patch the two collaborators every pool-posting test needs. `discord.File`
    becomes a plain `("FILE", filename)` tuple -- which the fake threads and
    `_attachment_names` both read -- and `PileImageBuilder().build` returns no
    image, since the .txt is the deliverable and the .jpg is best-effort. Pass
    `build=` for the tests that are specifically about the image."""
    with patch("services.draft_log_store.discord.File", lambda fp, filename=None: ("FILE", filename)), \
         patch("services.draft_log_store.PileImageBuilder") as PIB:
        PIB.return_value.build = build or AsyncMock(return_value=None)
        yield


async def _empty_history(limit=None):
    """An async generator that yields nothing -- the default .history() for
    plain mock channels/threads that don't need the posted-check to find
    anything already there."""
    if False:  # pragma: no cover - never runs; just makes this a generator fn
        yield None


def _channel(name, cid=7000):
    ch = MagicMock()
    ch.id = cid
    ch.name = name
    ch.send = AsyncMock()
    ch.history = _empty_history
    return ch


def _summary_channel(name, thread=None, create_thread_error=None, cid=7000):
    """Channel stand-in for the resolve-or-create step: `channel.send()`
    resolves to a summary message whose `create_thread()` either resolves to
    `thread` or raises `create_thread_error` (simulating Discord refusing
    thread creation, e.g. missing Manage Threads). The same `channel.send`
    also serves the HTTPException fallback path's per-player posts -- its
    return value is unused there, so one AsyncMock covers both."""
    ch = _channel(name, cid=cid)
    summary = MagicMock()
    summary.delete = AsyncMock()
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
        self.delete_error = None      # set to make a message's .delete() raise
        self.send = AsyncMock(side_effect=self._do_send)

    async def _do_send(self, content=None, files=None):
        msg = MagicMock()
        msg.author = SimpleNamespace(id=_BOT_ID)   # a real send from the bot itself
        msg.attachments = [
            MagicMock(filename=(f[1] if isinstance(f, tuple) else getattr(f, "filename", None)))
            for f in (files or [])
        ]

        async def _delete():
            # A real delete leaves history, so the double's must too --
            # otherwise a test could never tell an orphan summary was removed.
            if self.delete_error is not None:
                raise self.delete_error
            # Unguarded on purpose: Discord raises NotFound on a second
            # delete, so a double-delete should surface here too.
            self._messages.remove(msg)

        msg.delete = AsyncMock(side_effect=_delete)
        self._messages.append(msg)
        return msg

    async def history(self, limit=None):
        for msg in self._messages:
            yield msg

    def inject_foreign_message(self, filename, author_id=424242):
        """Simulate a message some OTHER user posted directly in the
        thread/channel (e.g. uploading their own decklist) -- appended
        straight to history, bypassing .send(), since it never came from the
        bot. Used to prove the posted-check ignores non-bot attachments."""
        msg = MagicMock()
        msg.author = SimpleNamespace(id=author_id)
        msg.attachments = [MagicMock(filename=filename)]
        self._messages.append(msg)

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


class _TagRefusingThread(_FakeThread):
    """Refuses the opening team tag (the one send carrying no files) but
    accepts every pool post."""

    async def _do_send(self, content=None, files=None):
        if not files:
            raise discord.HTTPException(MagicMock(), "cannot mention here")
        return await super()._do_send(content=content, files=files)


class _FakeChannel(_FakeThread):
    """A team-channel stand-in for the no-thread fallback: like _FakeThread
    (.send() records real messages, .history() replays them so the fallback's
    posted-check is exercised for real), but its .send() also returns a
    summary-shaped message whose create_thread() can be made to fail --
    covering the resolve-or-create attempt that precedes the fallback."""

    def __init__(self, name, create_thread_error=None, thread=None, cid=7000):
        super().__init__(tid=cid)
        self.name = name
        self.create_thread_error = create_thread_error
        # The thread handed back once creation stops failing. Settable, so a
        # test can model a refusal that later clears -- a transient 5xx, or an
        # admin granting Manage Threads between reconciler ticks.
        self.thread = thread

    async def _do_send(self, content=None, files=None):
        msg = await super()._do_send(content=content, files=files)
        msg.create_thread = AsyncMock(
            side_effect=self.create_thread_error,
            return_value=self.thread,
        )
        return msg

    def summary_messages(self):
        """Messages carrying no attachment -- i.e. the "Drafted pools" headers.
        A refused thread must leave none of these behind.

        Lives on the CHANNEL double, not the thread one: inside a thread an
        attachment-free message is the team tag, so the same predicate there
        would quietly count the tag as an orphan header."""
        return [msg for msg in self._messages if not msg.attachments]


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
        team_a_pools_destination_id=None, team_b_pools_destination_id=None,
    )


def _team_ds3(friendly_id="ABC"):
    """DraftSession stand-in with a single 3-player team A and an empty team B."""
    return SimpleNamespace(
        session_id="sid", draft_id="ABC", guild_id="42", friendly_id=friendly_id,
        draft_data=_team3_log(), team_logs_posted_at=None,
        team_a=["disc_a", "disc_b", "disc_c"], team_b=[],
        sign_ups={"disc_a": "Alice", "disc_b": "Bob", "disc_c": "Carol"},
        channel_ids=[111],
        team_a_pools_destination_id=None, team_b_pools_destination_id=None,
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
    when get_channel misses -- raises NotFound by default (a clean "it's
    gone"); tests that need it to succeed register the thread in `channels`
    instead, and tests covering a transient lookup error override
    fetch_channel with a non-NotFound/Forbidden HTTPException explicitly.
    bot.user is fixed so the posted-check's bot-authorship scoping matches
    every fake channel/thread's messages."""
    guild = MagicMock()
    guild.get_channel = lambda cid: channels.get(cid)
    bot = MagicMock()
    bot.user = SimpleNamespace(id=_BOT_ID)
    bot.get_guild.return_value = guild
    bot.get_channel = lambda cid: channels.get(cid)
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "not found"))
    return bot


def _attachment_names(destination):
    """Filenames of every attachment across all `destination.send` calls, in
    order. These tests patch discord.File to a ("FILE", filename) tuple. The
    one-off summary send carries no `files` kwarg at all."""
    return [
        f[1] for call in destination.send.await_args_list for f in call.kwargs.get("files", [])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("red_name, blue_name", [
    ("Red-Team-Chat-ABC", "Blue-Team-Chat-ABC"),
    # Discord stores text-channel names lowercased in production, so channel
    # matching must be case-insensitive or post_team_logs silently posts
    # nothing in prod.
    ("red-team-chat-abc", "blue-team-chat-abc"),
])
async def test_post_team_logs_scopes_pools_to_own_team_and_stamps(red_name, blue_name):
    ds = _team_ds()
    _, ctx = _db_ctx(ds)
    red_thread, blue_thread = _FakeThread(1001), _FakeThread(2002)
    red = _summary_channel(red_name, thread=red_thread)
    blue = _summary_channel(blue_name, thread=blue_thread)
    bot = _bot_for({111: red, 222: blue})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_logs_posted_at is not None
    # Thread ids persisted onto the session for a retry to find.
    assert ds.team_a_pools_destination_id == "1001"
    assert ds.team_b_pools_destination_id == "2002"
    # One summary message per team channel; the pools themselves land in the thread.
    assert red.send.await_count == 1
    assert blue.send.await_count == 1
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

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord(AsyncMock(return_value=io.BytesIO(b"\xff\xd8jpg"))):
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

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord(AsyncMock(side_effect=RuntimeError("scryfall down"))):
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

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        ok = await post_team_logs("sid", bot)

    assert ok is False
    assert ds.team_logs_posted_at is None
    assert ds.team_a_pools_destination_id is None
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

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        results = await asyncio.gather(
            post_team_logs("sid", bot),
            post_team_logs("sid", bot),
        )

    assert sorted(results) == [False, True]          # one ran, one was dropped
    assert red.send.await_count == 1                 # one summary message, not two
    assert blue.send.await_count == 1
    # One team tag + one pool post each; the duplicate call added neither.
    assert red_thread.send.await_count == 2
    assert blue_thread.send.await_count == 2


@pytest.mark.asyncio
async def test_post_team_logs_three_player_team_gets_one_summary_and_three_thread_posts():
    ds = _team_ds3()
    _, ctx = _db_ctx(ds)
    thread = _FakeThread(9001)
    red = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({111: red})

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        ok = await post_team_logs("sid", bot)

    assert ok is True
    assert ds.team_a_pools_destination_id == "9001"
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
    persist, persisted = _persist_recorder()

    with _patched_discord():
        first_ok = await _post_pools(bot, channel, persist, members=["disc_a", "disc_b", "disc_c"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)
        assert first_ok is True

        channel.send.reset_mock()   # only care whether run 2 opens a NEW summary/thread
        bot2 = _bot_for({9001: thread})   # second run resolves the stored id via bot.get_channel

        second_ok = await _post_pools(bot2, channel, persist, destination_id=persisted[-1], members=["disc_a", "disc_b", "disc_c"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

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

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        first_ok = await post_team_logs("sid", bot)

    assert first_ok is False
    assert ds.team_logs_posted_at is None
    assert ds.team_a_pools_destination_id == "9001"                          # persisted despite the failure
    assert thread.posted_txt_filenames() == ["Alice.txt", "Carol.txt"]  # Bob missing

    # Retry: the stored thread id must resolve (bot.get_channel) so the run
    # resumes into it rather than opening a second one; Bob's earlier hiccup
    # doesn't recur.
    bot2 = _bot_for({111: red, 9001: thread})
    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        second_ok = await post_team_logs("sid", bot2)

    assert second_ok is True
    assert ds.team_logs_posted_at is not None
    assert ds.team_a_pools_destination_id == "9001"                # no second thread created
    assert red.send.await_count == 1                          # one summary across both runs
    assert thread.posted_txt_filenames() == ["Alice.txt", "Carol.txt", "Bob.txt"]   # only Bob added


@pytest.mark.asyncio
async def test_post_pools_for_team_falls_back_to_channel_posts_when_thread_creation_refused():
    """If thread creation raises discord.HTTPException (typically missing
    Manage Threads), fall back to posting the per-player messages directly
    into the channel exactly as before threading existed."""
    channel = _summary_channel(
        "Red-Team-Chat-ABC",
        create_thread_error=discord.HTTPException(MagicMock(), "no Manage Threads"),
    )
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        all_posted = await _post_pools(bot, channel, persist, members=["disc_a"])

    assert all_posted is True
    assert persisted == ["7000"]     # the channel carried the pool, so it is the record
    channel.summary.create_thread.assert_awaited_once()
    # Both the summary attempt and the per-player fallback post went straight
    # to the channel: two sends total, the second carrying Alice's pool.
    assert channel.send.await_count == 2
    assert _attachment_names(channel) == ["Alice.txt"]


@pytest.mark.asyncio
async def test_post_pools_for_team_ignores_non_bot_txt_attachments_when_checking_who_posted():
    """A player uploading their own decklist as e.g. 'Alice.txt' into the
    thread must not be mistaken for the bot's own pool post -- otherwise
    Alice's real pool is silently skipped and the run reports her done."""
    thread = _FakeThread(9001)
    thread.inject_foreign_message("Alice.txt")   # someone else's upload, not the bot's post
    channel = _summary_channel("Red-Team-Chat-ABC")   # thread already stored; no creation needed
    bot = _bot_for({9001: thread})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        all_posted = await _post_pools(bot, channel, persist, destination_id="9001")

    assert all_posted is True
    channel.send.assert_not_awaited()             # thread already resolved -- no new summary
    assert persisted == []                        # resumed into the stored thread; nothing new
    # Alice's real pool WAS posted by the bot despite the foreign 'Alice.txt'
    # already sitting in the thread's history.
    assert _attachment_names(thread) == ["Alice.txt", "Bob.txt"]


@pytest.mark.asyncio
async def test_post_pools_for_team_tags_the_whole_team_first_and_only_on_creation():
    """The thread opens by mentioning the team -- that mention is what adds
    them to the thread, which is what puts it in their sidebar. It must come
    before the pools, cover the whole roster (including a member with no pool
    of their own), and never fire again on a resume."""
    draft_data = _team_log()
    sign_ups = {"disc_a": "Alice", "disc_b": "Bob"}
    mapping = {"disc_a": "dm_a"}                  # Bob has no mapped pool...
    thread = _FakeThread(9001)
    channel = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        first_ok = await _post_pools(bot, channel, persist, mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

    assert first_ok is True
    tag = thread.send.await_args_list[0]
    content = tag.kwargs.get("content") or tag.args[0]
    assert "<@disc_a>" in content and "<@disc_b>" in content   # ...but is still tagged
    assert "ABC" in content
    assert not tag.kwargs.get("files")             # the tag carries no pool of its own
    assert _attachment_names(thread) == ["Alice.txt"]

    # A resume into the same thread must not tag the team a second time.
    bot2 = _bot_for({9001: thread})
    with _patched_discord():
        second_ok = await _post_pools(bot2, channel, persist, destination_id=persisted[-1], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

    assert second_ok is True
    assert thread.send.await_count == 2            # the tag + Alice, nothing added


@pytest.mark.asyncio
async def test_post_pools_for_team_still_posts_pools_when_the_team_tag_fails():
    """Tagging is best-effort: a failed mention costs the team their sidebar
    entry, not their pools, and must not leave the run looking incomplete."""
    draft_data = _team_log()
    sign_ups = {"disc_a": "Alice"}
    mapping = {"disc_a": "dm_a"}
    thread = _TagRefusingThread(9001)
    channel = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        all_posted = await _post_pools(bot, channel, persist, members=["disc_a"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

    assert all_posted is True                      # the tag failure is not a delivery failure
    assert persisted == ["9001"]
    assert _attachment_names(thread) == ["Alice.txt"]


@pytest.mark.asyncio
async def test_post_pools_for_team_disambiguates_names_that_sanitise_identically():
    """'Bob!' and 'Bob?' both sanitise to 'Bob' -- both players must still be
    delivered under distinct filenames, and a retry must not mistake one's
    post for the other's."""
    draft_data = _collision_log()
    sign_ups = {"disc_x": "Bob!", "disc_y": "Bob?"}
    mapping = {"disc_x": "dm_x", "disc_y": "dm_y"}
    thread = _FakeThread(9001)
    channel = _summary_channel("Red-Team-Chat-ABC", thread=thread)
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        first_ok = await _post_pools(bot, channel, persist, members=["disc_x", "disc_y"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)
        assert first_ok is True

        filenames = thread.posted_txt_filenames()
        assert len(filenames) == 2
        assert len(set(filenames)) == 2            # both delivered, under distinct names

        # Retry with the same thread must not skip either player.
        channel.send.reset_mock()
        bot2 = _bot_for({9001: thread})
        second_ok = await _post_pools(bot2, channel, persist, destination_id=persisted[-1], members=["disc_x", "disc_y"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

    assert second_ok is True
    channel.send.assert_not_awaited()              # no second thread
    # One team tag + two pool posts on the first run; the retry added nothing.
    assert thread.send.await_count == 3


@pytest.mark.asyncio
async def test_post_pools_for_team_forbidden_destination_does_not_open_a_second_thread():
    """Forbidden means "I cannot see it", NOT "it is gone". The stored thread
    may be perfectly alive with everyone's pools already in it -- creating a
    second one would duplicate every pool and strand the first."""
    channel = _summary_channel("Red-Team-Chat-ABC", thread=_FakeThread(9002))
    bot = _bot_for({})
    bot.fetch_channel = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access")
    )
    persist, persisted = _persist_recorder()

    with _patched_discord():
        all_posted = await _post_pools(bot, channel, persist, destination_id="9001")

    assert all_posted is False          # unresolved, so the stamp stays unset
    channel.send.assert_not_awaited()   # no summary => no second thread
    assert persisted == []


@pytest.mark.asyncio
async def test_post_team_logs_transient_thread_lookup_error_does_not_open_second_thread_or_stamp():
    """A stored thread id whose lookup raises a non-NotFound/Forbidden
    HTTPException (a transient 5xx/timeout -- exactly the blip that made
    this a retry) must abort the run rather than read it as 'thread is
    gone', which would open a second thread alongside a first one that may
    still be perfectly alive."""
    ds = _team_ds(team_b=(), channel_ids=(111,))
    ds.team_a_pools_destination_id = "9001"   # a thread already exists from an earlier run
    _, ctx = _db_ctx(ds)
    red = _channel("Red-Team-Chat-ABC")
    bot = _bot_for({111: red})   # 9001 not cached -> get_channel misses, fetch_channel is hit
    bot.fetch_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "server error"))

    with patch("services.draft_log_store.db_session", MagicMock(return_value=ctx)), _patched_discord():
        ok = await post_team_logs("sid", bot)

    assert ok is False
    assert ds.team_logs_posted_at is None
    assert ds.team_a_pools_destination_id == "9001"     # unchanged -- no second thread created
    red.send.assert_not_awaited()                  # no summary posted; no fallback attempted either


@pytest.mark.asyncio
async def test_post_pools_for_team_still_posts_pools_when_the_orphan_summary_cannot_be_deleted():
    """Taking the orphan summary back down is best-effort. A guild that also
    refuses the delete gets a stray header -- annoying, but the pools are the
    deliverable and must still go out."""
    draft_data = _team_log()
    sign_ups = {"disc_a": "Alice"}
    mapping = {"disc_a": "dm_a"}
    channel = _FakeChannel(
        "Red-Team-Chat-ABC",
        create_thread_error=discord.HTTPException(MagicMock(), "no Manage Threads"),
    )
    channel.delete_error = discord.HTTPException(MagicMock(), "no Manage Messages")
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        all_posted = await _post_pools(bot, channel, persist, members=["disc_a"], mapping=mapping, draft_data=draft_data, sign_ups=sign_ups)

    assert all_posted is True
    assert channel.posted_txt_filenames() == ["Alice.txt"]   # the pool went out regardless
    assert len(channel.summary_messages()) == 1              # the header we could not remove
    assert persisted == ["7000"]     # the channel carried the pool, so it is the record


@pytest.mark.asyncio
async def test_post_pools_for_team_resumes_into_the_channel_the_fallback_claimed():
    """Thread creation is refused, so the pools go into the channel and the
    CHANNEL becomes this team's recorded destination. The other team then
    fails, so nothing is stamped and the reconciler retries -- and by now
    thread creation would succeed. It must not: opening a thread here would
    either duplicate the pools into it or, once deduped, leave the team tagged
    into a thread containing nothing. Their pools live in the channel; that is
    where the retry belongs."""
    channel = _FakeChannel(
        "Red-Team-Chat-ABC",
        create_thread_error=discord.HTTPException(MagicMock(), "transient"),
    )
    bot = _bot_for({7000: channel})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        first_ok = await _post_pools(bot, channel, persist)
    assert first_ok is True
    assert channel.posted_txt_filenames() == ["Alice.txt", "Bob.txt"]   # delivered in-channel
    assert persisted == ["7000"]                    # the channel is the record

    # The retry, resuming from the stored destination. Thread creation would
    # work now -- the point is that it is never attempted.
    thread = _FakeThread(9001)
    channel.create_thread_error = None
    channel.thread = thread
    channel.send.reset_mock()

    with _patched_discord():
        second_ok = await _post_pools(bot, channel, persist, destination_id=persisted[-1])

    assert second_ok is True
    assert persisted == ["7000"]                    # unchanged; nothing new claimed
    channel.send.assert_not_awaited()               # no summary, so no thread either
    assert _attachment_names(thread) == []          # the thread was never used
    assert channel.posted_txt_filenames() == ["Alice.txt", "Bob.txt"]   # nobody re-posted


@pytest.mark.asyncio
async def test_post_pools_for_team_fallback_does_not_repost_a_player_already_in_the_channel():
    """If thread creation stays refused, a retry must not re-post players the
    fallback already delivered straight into the channel -- otherwise every
    postable player gets re-posted every tick for up to 72h."""
    channel = _FakeChannel(
        "Red-Team-Chat-ABC",
        create_thread_error=discord.HTTPException(MagicMock(), "no Manage Threads"),
    )
    bot = _bot_for({})
    persist, persisted = _persist_recorder()

    with _patched_discord():
        first_ok = await _post_pools(bot, channel, persist)
        assert first_ok is True
        assert channel.posted_txt_filenames() == ["Alice.txt", "Bob.txt"]
        assert channel.summary_messages() == []      # the orphan header was taken back down

        channel.send.reset_mock()
        second_ok = await _post_pools(bot, channel, persist)

    assert second_ok is True
    assert persisted == ["7000"]      # the channel itself is now the record
    # Only the (failed) summary attempt happened again -- nobody re-posted.
    assert channel.send.await_count == 1
    assert channel.posted_txt_filenames() == ["Alice.txt", "Bob.txt"]   # unchanged
    # ...and that attempt left nothing behind either, so orphan headers cannot
    # pile up across the reconciler's 72h of retries.
    assert channel.summary_messages() == []
