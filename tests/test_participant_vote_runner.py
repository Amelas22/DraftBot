"""One runner for "post a vote, wait, act on the result" -- and the leak it closes.

Four commands run a participant vote. Three of them registered the vote in an
ACTIVE_*_VOTES dict and then deleted it in TWO places: once at the end of the
happy path, once in an `except`. Anything that raised between the registration
and that try block skipped both, leaving the entry behind -- and every later
attempt in that process answers "there's already an active vote", permanently.

The gap is not hypothetical: the acknowledgement to the invoker sits in exactly
that window, and a Discord followup can fail (expired interaction, 5xx, a lost
gateway) without the command having done anything wrong.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.draft_control import run_participant_vote


def make_view(passed=True):
    view = MagicMock()
    view.complete.wait = AsyncMock()
    view.get_vote_result = MagicMock(return_value=(passed, 2, 3))
    view.start_timer = AsyncMock()
    view.generate_status_embed = AsyncMock()
    return view


def make_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


def make_ctx():
    ctx = MagicMock()
    ctx.followup.send = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_the_registry_is_cleared_when_the_acknowledgement_fails():
    """The leak. Registration happens, the ack raises, and the entry must not
    outlive the attempt -- otherwise the command is dead until a restart."""
    registry = {}
    ctx = make_ctx()
    ctx.followup.send = AsyncMock(side_effect=RuntimeError("interaction expired"))

    with pytest.raises(RuntimeError):
        await run_participant_vote(
            ctx, channel=make_channel(), view=make_view(), registry=registry,
            key="s1", announcement="vote!", ack="started",
            on_pass=AsyncMock(), on_fail=AsyncMock())

    assert registry == {}, (
        "the vote entry outlived a failed acknowledgement, so this command is "
        "now permanently blocked for this session")


@pytest.mark.asyncio
async def test_a_passing_vote_runs_only_the_pass_action():
    registry, on_pass, on_fail = {}, AsyncMock(), AsyncMock()
    await run_participant_vote(
        make_ctx(), channel=make_channel(), view=make_view(passed=True), registry=registry,
        key="s1", announcement="vote!", ack="started", on_pass=on_pass, on_fail=on_fail)

    on_pass.assert_awaited_once()
    on_fail.assert_not_awaited()
    assert registry == {}


@pytest.mark.asyncio
async def test_a_failing_vote_runs_only_the_fail_action():
    registry, on_pass, on_fail = {}, AsyncMock(), AsyncMock()
    await run_participant_vote(
        make_ctx(), channel=make_channel(), view=make_view(passed=False), registry=registry,
        key="s1", announcement="vote!", ack="started", on_pass=on_pass, on_fail=on_fail)

    on_fail.assert_awaited_once()
    on_pass.assert_not_awaited()
    assert registry == {}


@pytest.mark.asyncio
async def test_the_vote_is_registered_while_it_runs():
    """The registry is what stops a second concurrent vote, so the entry has to
    actually be present for the duration -- not merely added and removed."""
    registry = {}
    view = make_view()
    seen = {}

    async def note_it():
        seen["registered"] = registry.get("s1") is view

    view.complete.wait = AsyncMock(side_effect=note_it)
    await run_participant_vote(
        make_ctx(), channel=make_channel(), view=view, registry=registry,
        key="s1", announcement="vote!", ack="started",
        on_pass=AsyncMock(), on_fail=AsyncMock())

    assert seen.get("registered"), "the vote was not registered while it ran"
    assert registry == {}


@pytest.mark.asyncio
async def test_an_action_that_raises_still_clears_the_registry():
    """A failing on_pass must not wedge the command either."""
    registry = {}
    with pytest.raises(RuntimeError):
        await run_participant_vote(
            make_ctx(), channel=make_channel(), view=make_view(passed=True),
            registry=registry, key="s1", announcement="vote!", ack="started",
            on_pass=AsyncMock(side_effect=RuntimeError("boom")),
            on_fail=AsyncMock())

    assert registry == {}


@pytest.mark.asyncio
async def test_a_second_vote_for_the_same_session_is_refused():
    """The registry is the interlock. It only works if the check and the claim
    happen with no await between them -- each command used to check, then build a
    view, generate an embed and post a message (three awaits) before claiming the
    slot, so two invocations could both pass the check and both post."""
    registry = {}
    first, second = make_view(), make_view()
    registry["s1"] = first                       # a vote already running

    posted = await run_participant_vote(
        make_ctx(), channel=make_channel(), view=second, registry=registry,
        key="s1", announcement="vote!", ack="started",
        on_pass=AsyncMock(), on_fail=AsyncMock())

    assert posted is False, "a second concurrent vote was allowed to start"
    assert registry["s1"] is first, "the running vote was overwritten"


@pytest.mark.asyncio
async def test_the_slot_is_claimed_before_anything_is_posted():
    """So a concurrent caller is refused even while this one is mid-post."""
    registry = {}
    channel = make_channel()
    claimed = {}

    async def check_during_send(*_a, **_k):
        claimed["at_send"] = registry.get("s1") is not None
        return MagicMock()

    channel.send = AsyncMock(side_effect=check_during_send)
    await run_participant_vote(
        make_ctx(), channel=channel, view=make_view(), registry=registry,
        key="s1", announcement="vote!", ack="started",
        on_pass=AsyncMock(), on_fail=AsyncMock())

    assert claimed.get("at_send"), "the slot was still free while the vote was being posted"


@pytest.mark.asyncio
async def test_the_announcement_goes_to_the_channel_it_was_given():
    channel = make_channel()
    await run_participant_vote(
        make_ctx(), channel=channel, view=make_view(), registry={}, key="s1",
        announcement="⚠️ vote text", ack="started",
        on_pass=AsyncMock(), on_fail=AsyncMock())

    assert channel.send.await_args.args[0] == "⚠️ vote text"


@pytest.mark.asyncio
async def test_no_vote_is_posted_if_the_invoker_cannot_be_acknowledged():
    """Posting before acknowledging leaves an orphan when the ack fails: the vote
    is visible and clickable, but nothing is awaiting its result, so it passes and
    does nothing. Acknowledge first -- then a failure means no vote was ever put
    in front of anyone."""
    ctx = make_ctx()
    ctx.followup.send = AsyncMock(side_effect=RuntimeError("interaction expired"))
    channel = make_channel()

    with pytest.raises(RuntimeError):
        await run_participant_vote(
            ctx, channel=channel, view=make_view(), registry={}, key="s1",
            announcement="vote!", ack="started",
            on_pass=AsyncMock(), on_fail=AsyncMock())

    assert channel.send.await_count == 0, (
        "a vote was posted that nothing is waiting on -- it can be clicked, it "
        "can pass, and it cannot act")
