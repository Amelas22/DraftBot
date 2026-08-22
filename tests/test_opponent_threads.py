from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers.opponent_threads import opponent_ids, own_team_ids, spawn_opponent_threads


def make_channel(existing_thread_names=(), archived_thread_names=(), starter_fails=False):
    """Fake TextChannel. create_thread returns a fresh fake thread each call and
    records it on `created_threads`, so tests can inspect what was sent into it.

    `threads` mirrors pycord's cached ACTIVE threads; archived ones are only
    reachable through the separate archived_threads() API call.
    """
    created_threads = []

    def new_thread(**kwargs):
        send = AsyncMock(side_effect=RuntimeError("cannot post")) if starter_fails else AsyncMock()
        thread = SimpleNamespace(name=kwargs.get("name"), send=send)
        created_threads.append(thread)
        return thread

    async def archived_threads(**kwargs):
        for name in archived_thread_names:
            yield SimpleNamespace(name=name)

    return SimpleNamespace(
        threads=[SimpleNamespace(name=n) for n in existing_thread_names],
        archived_threads=archived_threads,
        create_thread=AsyncMock(side_effect=new_thread),
        created_threads=created_threads,
    )


def thread_names(channel):
    """Names the helper asked Discord to create, in order."""
    return [c.kwargs["name"] for c in channel.create_thread.await_args_list]


def test_red_team_channel_gets_team_b_as_opponents():
    assert opponent_ids("Red-Team", ["a1", "a2"], ["b1", "b2"]) == ["b1", "b2"]


def test_blue_team_channel_gets_team_a_as_opponents():
    assert opponent_ids("Blue-Team", ["a1", "a2"], ["b1", "b2"]) == ["a1", "a2"]


def test_shared_draft_channel_has_no_opponents():
    """The 'Draft' channel holds both teams, so nobody in it is an opponent."""
    assert opponent_ids("Draft", ["a1"], ["b1"]) == []


def test_unknown_team_name_has_no_opponents():
    assert opponent_ids("Green-Team", ["a1"], ["b1"]) == []


def test_missing_team_rosters_yield_no_opponents():
    """team_a/team_b default to None in create_team_channel's signature."""
    assert opponent_ids("Red-Team", None, None) == []


SIGN_UPS = {"a1": "Alice", "b1": "Dave", "b2": "Erin"}


@pytest.mark.asyncio
async def test_creates_one_named_thread_per_opponent():
    channel = make_channel()
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], SIGN_UPS
    )
    assert created == 2
    names = thread_names(channel)
    assert names == ["Dave", "Erin"]


@pytest.mark.asyncio
async def test_thread_starter_names_the_opponent_and_their_team():
    channel = make_channel()
    await spawn_opponent_threads(channel, "Red-Team", ["a1"], ["b1"], SIGN_UPS)
    (thread,) = channel.created_threads
    starter = thread.send.await_args.args[0]
    assert "Dave" in starter
    assert "Blue Team" in starter


def test_own_team_ids_is_the_mirror_of_opponent_ids():
    assert own_team_ids("Red-Team", ["a1", "a2"], ["b1"]) == ["a1", "a2"]
    assert own_team_ids("Blue-Team", ["a1", "a2"], ["b1"]) == ["b1"]
    assert own_team_ids("Not-A-Team", ["a1"], ["b1"]) == []


@pytest.mark.asyncio
async def test_starter_tags_the_owning_team_and_never_the_opponent():
    """The mention is what adds the team to the thread, which is what puts it
    in their sidebar. The scouted player is on the other team and cannot see
    this channel, so tagging them would be useless and rude."""
    channel = make_channel()
    await spawn_opponent_threads(channel, "Red-Team", ["a1", "a2"], ["b1"], SIGN_UPS)
    (thread,) = channel.created_threads
    starter = thread.send.await_args.args[0]

    assert "<@a1>" in starter and "<@a2>" in starter    # the whole owning team
    assert "<@b1>" not in starter                       # never the scouted opponent
    # Mentions lead, so the tag is the first thing in the thread.
    assert starter.startswith("<@a1> <@a2> ")
    # Silent: one thread per opponent would otherwise ping the team once each.
    # A silent mention still adds them to the thread (verified against Discord),
    # so this must not regress to a loud send.
    assert thread.send.await_args.kwargs.get("silent") is True


@pytest.mark.asyncio
async def test_starter_tags_blue_teams_own_roster_in_its_own_channel():
    channel = make_channel()
    await spawn_opponent_threads(channel, "Blue-Team", ["a1"], ["b1", "b2"], SIGN_UPS)
    (thread,) = channel.created_threads
    starter = thread.send.await_args.args[0]

    assert "<@b1>" in starter and "<@b2>" in starter
    assert "<@a1>" not in starter


@pytest.mark.asyncio
async def test_starter_without_a_resolvable_own_team_still_posts():
    """An empty roster must not produce a stray leading space or drop the
    scouting text -- the thread is still useful untagged."""
    channel = make_channel()
    await spawn_opponent_threads(channel, "Red-Team", [], ["b1"], SIGN_UPS)
    (thread,) = channel.created_threads
    starter = thread.send.await_args.args[0]

    assert starter.startswith("🔍 Scouting thread for")
    assert "Dave" in starter


@pytest.mark.asyncio
async def test_skips_opponents_that_already_have_a_thread():
    """Reruns (recover_draft_channels) must not double up."""
    channel = make_channel(existing_thread_names=["Dave"])
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], SIGN_UPS
    )
    assert created == 1
    names = thread_names(channel)
    assert names == ["Erin"]


@pytest.mark.asyncio
async def test_one_failed_thread_does_not_block_the_others():
    channel = make_channel()
    channel.create_thread = AsyncMock(
        side_effect=[RuntimeError("no perms"), SimpleNamespace(send=AsyncMock())]
    )
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], SIGN_UPS
    )
    assert created == 1


@pytest.mark.asyncio
async def test_shared_draft_channel_creates_nothing():
    channel = make_channel()
    created = await spawn_opponent_threads(
        channel, "Draft", ["a1"], ["b1"], SIGN_UPS
    )
    assert created == 0
    channel.create_thread.assert_not_called()


@pytest.mark.asyncio
async def test_two_opponents_with_the_same_display_name_each_get_a_thread():
    """Discord display names are not unique. If both opponents resolve to the
    same thread name, the already-exists check silently swallows the second one
    and that player ends up with no scouting thread at all."""
    sign_ups = {"b1": "Dave", "b2": "Dave"}
    channel = make_channel()
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], sign_ups
    )
    assert created == 2
    names = thread_names(channel)
    assert len(set(names)) == 2, f"opponents collapsed onto one thread name: {names}"


@pytest.mark.asyncio
async def test_long_display_name_fits_discord_thread_name_limit():
    """Discord rejects thread names over 100 characters with a 400."""
    sign_ups = {"b1": "M" + "a" * 200 + "n"}
    channel = make_channel()
    await spawn_opponent_threads(channel, "Red-Team", ["a1"], ["b1"], sign_ups)
    (name,) = thread_names(channel)
    assert 0 < len(name) <= 100


@pytest.mark.asyncio
async def test_names_are_stable_so_a_rerun_creates_nothing():
    """recover_draft_channels re-runs this against a rebuilt channel. Names must
    be reproducible or the skip check won't recognise what it already made."""
    sign_ups = {"b1": "Dave", "b2": "Dave"}
    first = make_channel()
    await spawn_opponent_threads(first, "Red-Team", ["a1"], ["b1", "b2"], sign_ups)
    made = thread_names(first)

    second = make_channel(existing_thread_names=made)
    created = await spawn_opponent_threads(
        second, "Red-Team", ["a1"], ["b1", "b2"], sign_ups
    )
    assert created == 0


@pytest.mark.asyncio
async def test_blank_display_name_falls_back_to_a_usable_label():
    """Discord rejects an empty thread name; a whitespace-only nickname must not
    become one."""
    channel = make_channel()
    await spawn_opponent_threads(channel, "Red-Team", ["a1"], ["b1"], {"b1": "   "})
    (name,) = thread_names(channel)
    assert name.strip(), f"blank thread name: {name!r}"


@pytest.mark.asyncio
async def test_never_raises_when_the_channel_itself_misbehaves():
    """views.py create_team_channel calls this with no guard of its own, on the
    draft's critical path -- a raise here would strand the session at the
    channel-creation step."""
    class Hostile:
        create_thread = AsyncMock()

        @property
        def threads(self):
            raise RuntimeError("channel state unavailable")

    created = await spawn_opponent_threads(
        Hostile(), "Red-Team", ["a1"], ["b1"], SIGN_UPS
    )
    assert created == 0


@pytest.mark.asyncio
async def test_thread_counts_even_if_its_starter_message_fails():
    """The thread exists the moment create_thread returns. Treating a failed
    starter as a failed creation loses track of a thread that is really there."""
    channel = make_channel(starter_fails=True)
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1"], SIGN_UPS
    )
    assert created == 1
    assert thread_names(channel) == ["Dave"]


@pytest.mark.asyncio
async def test_skips_opponents_whose_thread_has_been_archived():
    """channel.threads holds only ACTIVE threads. After the 3-day auto-archive,
    a rerun that consulted it alone would create a duplicate."""
    channel = make_channel(archived_thread_names=["Dave"])
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], SIGN_UPS
    )
    assert created == 1
    assert thread_names(channel) == ["Erin"]


@pytest.mark.asyncio
async def test_archived_lookup_failure_degrades_to_the_active_thread_list():
    """The archived fetch is an API call and may fail; it must not cost us the
    threads we can still create."""
    channel = make_channel(existing_thread_names=["Dave"])

    def boom(**kwargs):
        raise RuntimeError("missing read_message_history")

    channel.archived_threads = boom
    created = await spawn_opponent_threads(
        channel, "Red-Team", ["a1"], ["b1", "b2"], SIGN_UPS
    )
    assert created == 1
    assert thread_names(channel) == ["Erin"]
