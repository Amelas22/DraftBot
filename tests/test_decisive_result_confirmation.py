"""A report that ends the draft has to be confirmed before it is written.

Settlement is one-way. `utils.generate_draft_summary_embed` books the stake
debts the moment a victory is recorded, and `check_and_post_victory_or_draw`
then refuses to reprocess the session -- so a correction afterwards moves the
record and never the money. That is how `icebind-pillar-36` paid out five debts
on a match that was a draw.

The fix is a checkpoint on the ONE path that writes a reported winner
(`MatchResultSelect`). These pin two things: that the bot can tell in advance
whether a selection ends the draft, and that a selection which does is not
written until someone says yes.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from session import AsyncSessionLocal, DraftSession, MatchResult
from conftest import seed_session
from views import MatchResultSelect

A = ["a1", "a2", "a3", "a4"]
B = ["b1", "b2", "b3", "b4"]

# Selection values, as MatchResultSelect builds them: p1wins-p2wins-winner
P1_WINS = "2-0-1"
P2_WINS = "0-2-2"
NO_MATCH = "0-0-0"


async def _seed(winners, *, total_matches):
    """Seed a draft whose LAST match is the one under test.

    `winners` holds one entry per match row: "a", "b", or None for a match that
    exists but has not been reported. Teams are four players who meet
    repeatedly, which is all the win count cares about.
    """
    rows = []
    for i, w in enumerate(winners):
        p1, p2 = A[i % 4], B[i % 4]
        rows.append((p1, p2, {"a": p1, "b": p2, None: None}[w], None))
    await seed_session("s1", stage="pairings", teams=(A, B), matches=rows)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "s1")
                        .values(match_counter=total_matches + 1))
        await s.commit()
    return len(rows)


async def _outcome(select, value):
    """What the select says recording `value` would settle."""
    outcome, *_ = await select._projected(value)
    return outcome


def _select(match_number, chose=None):
    sel = MatchResultSelect(bot=MagicMock(), match_number=match_number,
                            session_id="s1", player1_name="P1", player2_name="P2")
    if chose is not None:
        # py-cord returns None from .values until the select has been used.
        sel._interaction = MagicMock()
        sel._selected_values = [chose]
    return sel


async def _winner_in_db(match_number):
    async with AsyncSessionLocal() as s:
        row = await s.execute(select(MatchResult).where(
            MatchResult.session_id == "s1",
            MatchResult.match_number == match_number))
        return row.scalar_one().winner_id


# --- can the bot see it coming? ---------------------------------------------

@pytest.mark.asyncio
async def test_a_report_that_clinches_is_recognised(test_db):
    """4-4 of nine, and this match hands it to A."""
    n = await _seed(["a"] * 4 + ["b"] * 4 + [None], total_matches=9)
    assert await _outcome(_select(n), P1_WINS) == "team_a"


@pytest.mark.asyncio
async def test_a_report_that_leaves_the_draft_live_is_not_flagged(test_db):
    """3-4 becoming 4-4 of NINE decides nothing -- a match is still owed."""
    n = await _seed(["a"] * 3 + ["b"] * 4 + [None], total_matches=9)
    assert await _outcome(_select(n), P1_WINS) is None


@pytest.mark.asyncio
async def test_the_final_match_of_an_even_draft_can_be_a_draw(test_db):
    """3-4 of eight, and this match squares it. A draw ends the draft too, and
    ends it paying nobody -- which is the outcome icebind-pillar-36 should have
    reached before five debts were booked."""
    n = await _seed(["a"] * 3 + ["b"] * 4 + [None], total_matches=8)
    assert await _outcome(_select(n), P1_WINS) == "draw"


@pytest.mark.asyncio
async def test_flipping_an_already_reported_winner_swings_two(test_db):
    """The icebind shape. The last match stands as an A win at 5-3 of eight;
    correcting it to B is 4-4, not 5-4. Treating a correction as a fresh win
    would show the person confirming an outcome that cannot happen."""
    n = await _seed(["a"] * 4 + ["b"] * 3 + ["a"], total_matches=8)
    assert await _outcome(_select(n), P2_WINS) == "draw"


@pytest.mark.asyncio
async def test_a_correction_can_un_decide_the_draft(test_db):
    """5-3 of eight is settled; voiding one of those wins puts the draft back in
    play, so there is nothing to confirm."""
    n = await _seed(["a"] * 4 + ["b"] * 3 + ["a"], total_matches=8)
    assert await _outcome(_select(n), NO_MATCH) is None


@pytest.mark.asyncio
async def test_restating_the_same_winner_does_not_invent_a_clinch(test_db):
    """2-0 corrected to 2-1 keeps the winner, so the score does not move."""
    n = await _seed(["a"] * 3 + ["b"] * 4 + ["a"], total_matches=9)
    assert await _outcome(_select(n), P1_WINS) is None


# --- and does it actually withhold the write? -------------------------------

def _interaction():
    i = MagicMock()
    i.user.id = 4242
    i.guild_id = 1
    i.response.defer = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.send_message = AsyncMock()
    i.edit_original_response = AsyncMock()
    i.followup.send = AsyncMock()
    return i


@pytest.mark.asyncio
async def test_a_decisive_selection_writes_nothing_until_confirmed(test_db):
    """The whole point: the row is untouched, so no settlement can have run."""
    n = await _seed(["a"] * 4 + ["b"] * 4 + [None], total_matches=9)
    interaction = _interaction()

    with patch("views.check_and_post_victory_or_draw", new=AsyncMock()) as settle:
        await _select(n, chose=P1_WINS).callback(interaction)

    assert await _winner_in_db(n) is None, (
        "the deciding result was written before anyone confirmed it")
    settle.assert_not_awaited()
    assert interaction.edit_original_response.await_count == 1, (
        "no confirmation was put in front of the reporter")
    assert interaction.response.defer.await_count == 1, (
        "the interaction was not acknowledged before the database was read -- "
        "Discord expires an unanswered interaction after three seconds")


@pytest.mark.asyncio
async def test_an_ordinary_selection_is_written_immediately(test_db):
    """Most matches decide nothing. Gating every report would tax the common
    case, so only the deciding one asks a second time."""
    n = await _seed(["a"] * 3 + ["b"] * 4 + [None], total_matches=9)
    interaction = _interaction()
    sel = _select(n, chose=P1_WINS)
    sel.update_pairings_posting = AsyncMock()

    with patch("views.check_and_post_victory_or_draw", new=AsyncMock()), \
         patch("views.update_draft_summary_message", new=AsyncMock()), \
         patch("livedrafts.update_live_draft_summary", new=AsyncMock()):
        await sel.callback(interaction)

    assert await _winner_in_db(n) == A[3], "an ordinary report was not recorded"
    assert interaction.edit_original_response.await_count == 0, (
        "an ordinary report asked for confirmation")


@pytest.mark.asyncio
async def test_an_expired_confirmation_says_so():
    """A stopped view keeps its buttons drawn, and a click on one gets a bare
    "interaction failed" from Discord. Left like that the reporter cannot tell
    whether the result went in -- the same dead end as a dialog with no way
    back, only five minutes later."""
    from views import ConfirmDecisiveResultView

    origin = MagicMock()
    origin.edit_original_response = AsyncMock()
    view = ConfirmDecisiveResultView(MagicMock(), P1_WINS, 1, origin)

    await view.on_timeout()

    said = origin.edit_original_response.await_args.kwargs
    assert "nothing was recorded" in said["content"].lower(), (
        f"an expired confirmation did not say the result was never recorded: {said}")
    assert said["view"] is None, "the dead buttons were left on screen"


@pytest.mark.asyncio
async def test_two_simultaneous_reports_cannot_end_the_draft_unasked(test_db):
    """The bypass the lock exists to close.

    At 3-3 of nine with two matches outstanding, each report on its own takes
    the score to 4-3 and decides nothing. Projected concurrently they BOTH look
    harmless, both skip the dialog, and the draft finishes 5-3 having asked
    nobody -- the guarantee silently gone in exactly the case it is needed.
    Projecting and writing under one per-draft lock forces the second report to
    see the first, so it is the one that gets stopped.
    """
    import asyncio

    await _seed(["a"] * 3 + ["b"] * 3 + [None, None], total_matches=9)

    async def report(match_number, interaction):
        sel = _select(match_number, chose=P1_WINS)
        sel.update_pairings_posting = AsyncMock()
        await sel.callback(interaction)

    first, second = _interaction(), _interaction()
    with patch("views.check_and_post_victory_or_draw", new=AsyncMock()), \
         patch("views.update_draft_summary_message", new=AsyncMock()), \
         patch("livedrafts.update_live_draft_summary", new=AsyncMock()):
        await asyncio.gather(report(7, first), report(8, second))

    dialogs = (first.edit_original_response.await_count
               + second.edit_original_response.await_count)
    assert dialogs == 1, (
        f"{dialogs} of the two concurrent reports asked for confirmation; exactly "
        f"one should -- the second report is the one that takes the draft to 5-3")


@pytest.mark.asyncio
async def test_a_failed_write_is_not_announced_as_recorded():
    """Confirm used to say "Result recorded." unconditionally. _apply_result
    handles its own errors, so the caller could not tell a write from a failure
    and cheerfully reported success over a missing match row."""
    from views import ConfirmDecisiveResultView

    select = MagicMock()
    select.session_id = "s1"
    select._apply_result = AsyncMock(return_value=False)
    interaction = _interaction()
    view = ConfirmDecisiveResultView(select, P1_WINS, interaction.user.id, interaction)

    await view.confirm_button.callback(interaction)

    said = interaction.edit_original_response.await_args.kwargs["content"]
    assert "could not be recorded" in said.lower(), (
        f"a failed write was announced as success: {said!r}")


@pytest.mark.asyncio
async def test_a_second_confirm_click_does_not_write_twice():
    """Both clicks are dispatched before the first finishes, and each would see
    an unreported match and run the first-report side effects -- rating updates
    and streak extensions among them."""
    from views import ConfirmDecisiveResultView

    select = MagicMock()
    select.session_id = "s1"
    select._apply_result = AsyncMock(return_value=True)
    interaction = _interaction()
    view = ConfirmDecisiveResultView(select, P1_WINS, interaction.user.id, interaction)

    await view.confirm_button.callback(interaction)
    await view.confirm_button.callback(_interaction())

    assert select._apply_result.await_count == 1, (
        f"the result was written {select._apply_result.await_count} times")
