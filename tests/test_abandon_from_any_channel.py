"""`/abandon` has to work from the channel a stalled draft is actually sitting in.

It resolved the draft with get_by_channel_id, which matches draft_chat_channel and
nothing else. But a draft stalls in the TEAM channels -- that is where players are
when a match never gets reported -- and running it there told them to go somewhere
else. get_by_any_channel_id already exists for this: it also searches channel_ids.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_draft_stub, sent_to_invoker

DRAFT_CHAT = 100
RED_TEAM_CHAT = 101


def make_ctx(channel_id, invoker_id="99"):   # 99 is in no draft
    ctx = MagicMock()
    ctx.author.id = int(invoker_id)
    ctx.channel_id = channel_id
    ctx.channel.id = channel_id
    ctx.channel.send = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.guild.get_member = lambda _id: None
    ctx.shared_chat = MagicMock()
    ctx.shared_chat.send = AsyncMock()
    ctx.guild.get_channel = lambda cid: ctx.shared_chat if cid == DRAFT_CHAT else None
    return ctx


@pytest.mark.asyncio
async def test_abandon_resolves_the_draft_from_a_team_channel(draft_control_cog):
    """The failure this fixes: a stalled draft lives in the team channels, and
    running /abandon there used to claim there was no draft here."""
    ctx = make_ctx(RED_TEAM_CHAT)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)):
        await draft_control_cog._do_abandon(ctx)

    # Resolved: it got as far as the participant check instead of "no draft here".
    assert "Only draft participants" in sent_to_invoker(ctx), (
        f"never resolved the draft from a team channel: {sent_to_invoker(ctx)!r}")


@pytest.mark.asyncio
async def test_abandon_still_works_from_the_draft_chat(draft_control_cog):
    """The channel it already supported must keep working."""
    ctx = make_ctx(DRAFT_CHAT)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)):
        await draft_control_cog._do_abandon(ctx)

    assert "Only draft participants" in sent_to_invoker(ctx)


@pytest.mark.asyncio
async def test_a_channel_belonging_to_no_draft_is_still_refused(draft_control_cog):
    """Widening the lookup must not make the command fire anywhere."""
    ctx = make_ctx(999)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=None)):
        await draft_control_cog._do_abandon(ctx)

    said = sent_to_invoker(ctx).lower()
    assert "no draft here" in said, f"expected a no-draft refusal, got: {said!r}"
    assert "draft chat" in said and "team channel" in said, (
        f"the refusal should name the channels that DO work, got: {said!r}")


@pytest.mark.asyncio
async def test_an_admin_is_offered_the_confirm_rather_than_an_immediate_void(draft_control_cog):
    """The admin arm. Also pins guard ORDER: the live-draft check sits above this,
    so an admin must not be able to void a draft people are still drafting."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="99")
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=True)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=False)):
        await draft_control_cog._do_abandon(ctx)

    view = ctx.followup.send.await_args.kwargs.get("view")
    assert view is not None and type(view).__name__ == "AbandonConfirmView", (
        f"an admin should be asked to confirm, got view={view!r}")


@pytest.mark.asyncio
async def test_an_admin_cannot_void_a_draft_that_is_still_being_drafted(draft_control_cog):
    """The order that matters: live check BEFORE the admin path."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="99")
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=True)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=True)):
        await draft_control_cog._do_abandon(ctx)

    said = sent_to_invoker(ctx)
    assert "/scrap" in said, f"a live draft should be sent to /scrap, got: {said!r}"
    assert ctx.followup.send.await_args.kwargs.get("view") is None, (
        "offered to void a draft that is still being drafted")


@pytest.mark.asyncio
async def test_a_participant_starts_a_vote(draft_control_cog):
    """The vote arm: a participant gets a vote posted in the channel, not a confirm."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="1")   # 1 is in sign_ups
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=False)), \
         patch("cogs.draft_control.AbandonVoteView") as vote_view:
        vote_view.return_value.generate_status_embed = AsyncMock()
        vote_view.return_value.complete.wait = AsyncMock()
        vote_view.return_value.get_vote_result = MagicMock(return_value=(False, 0, 2))
        vote_view.return_value.start_timer = AsyncMock()
        await draft_control_cog._do_abandon(ctx)

    # In the shared draft chat, never the channel it was typed in: the FIRST
    # send is the announcement, the last is the outcome.
    posted = [str(c.args[0]) for c in ctx.shared_chat.send.await_args_list if c.args]
    assert posted and "Abandonment Vote" in posted[0], (
        f"no vote reached the shared draft chat, got: {posted!r}")
    assert ctx.channel.send.await_count == 0, (
        "the vote leaked into the channel the command was typed in")


@pytest.mark.asyncio
async def test_the_vote_is_posted_where_every_participant_can_see_it(draft_control_cog):
    """Team channels are private to one team -- @everyone is denied read and only
    that team's members are granted it. A vote posted in Red-Team-Chat is invisible
    to Blue, so a majority of six can never be reached from three voters. The vote
    has to land in the shared draft chat however the command was reached."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="1")   # a participant, in a team channel
    shared = MagicMock()
    shared.send = AsyncMock()
    ctx.guild.get_channel = lambda cid: shared if cid == DRAFT_CHAT else None

    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting", new=MagicMock(return_value=False)), \
         patch("cogs.draft_control.AbandonVoteView") as vote_view:
        vote_view.return_value.generate_status_embed = AsyncMock()
        vote_view.return_value.complete.wait = AsyncMock()
        vote_view.return_value.get_vote_result = MagicMock(return_value=(False, 0, 2))
        vote_view.return_value.start_timer = AsyncMock()
        await draft_control_cog._do_abandon(ctx)

    assert shared.send.await_count, (
        "the vote never reached the shared draft chat, so half the participants "
        "could not have voted on it")
    posted = str(shared.send.await_args_list[0].args[0])
    assert "Abandonment Vote" in posted


@pytest.mark.asyncio
async def test_no_vote_is_started_when_the_shared_chat_cannot_be_found(draft_control_cog):
    """A vote only some participants can see is not a weaker vote, it is a wrong
    one -- in an uneven draft the larger team could pass it unseen. If there is
    nowhere every participant can read, refuse and send them to an admin rather
    than starting a vote in a team channel."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="1")      # participant, team channel
    ctx.guild.get_channel = lambda _cid: None          # shared chat gone

    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=False)):
        await draft_control_cog._do_abandon(ctx)

    assert ctx.channel.send.await_count == 0, (
        "started a vote in a team channel, where only one team could see it")
    said = sent_to_invoker(ctx)
    assert "admin" in said.lower(), f"gave the player no way forward: {said!r}"


@pytest.mark.asyncio
async def test_the_admin_notice_reaches_everyone_not_just_one_team(draft_control_cog):
    """Widening /abandon to team channels widened where its ANNOUNCEMENT lands too.
    An admin abandoning from red-team-chat voids all nine matches; if the notice
    goes to ctx.channel, Blue never learns why their results vanished and keeps
    trying to report a match that no longer exists."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="99")
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(
                   draft_chat_channel=str(DRAFT_CHAT),
                   channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=True)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=False)):
        await draft_control_cog._do_abandon(ctx)

    view = ctx.followup.send.await_args.kwargs.get("view")
    assert view is not None, "the admin was not offered the confirmation"
    assert view.channel is ctx.shared_chat, (
        "the admin notice would go to the team channel the command was typed in, "
        "so the other team never hears the draft was voided")


@pytest.mark.asyncio
async def test_a_refused_abandon_is_not_announced_as_a_successful_one(draft_control_cog):
    """abandon_draft_session refuses when the draft finished while the vote ran.
    Announcing "all match results have been voided" anyway tells six players their
    results are gone when they are not -- they will go and re-report matches that
    never needed re-reporting."""
    ctx = make_ctx(RED_TEAM_CHAT, invoker_id="1")
    captured = {}

    async def capture_vote(_ctx, **kw):
        captured["on_pass"] = kw["on_pass"]
        return True

    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=make_draft_stub(
                   draft_chat_channel=str(DRAFT_CHAT), channel_ids=[DRAFT_CHAT, RED_TEAM_CHAT]))), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch("cogs.draft_control.DraftSetupManager.is_drafting",
               new=MagicMock(return_value=False)), \
         patch("cogs.draft_control.run_participant_vote", new=capture_vote), \
         patch("cogs.draft_control.abandon_draft_session",
               new=AsyncMock(return_value=False)):          # it refused
        await draft_control_cog._do_abandon(ctx)
        await captured["on_pass"]()

    said = " ".join(str(c.args[0]) for c in ctx.shared_chat.send.await_args_list if c.args)
    assert said, "said nothing at all about the outcome"
    # The false claim, not the word: "Nothing was voided" is the correct message.
    assert "results have been voided" not in said.lower(), (
        f"told players their results were voided when they were not: {said!r}")
    assert "results stand" in said.lower(), (
        f"did not tell players their results survived: {said!r}")
