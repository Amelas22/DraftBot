"""Would THIS report end the draft?

The bot settles stakes the moment a report makes the draft final, and that
settlement is one-way: `check_and_post_victory_or_draw` guards itself with
`already_processed`, so a later correction changes the record and never the
money. On 2026-08-28 draft `icebind-pillar-36` had match 9 reported as a team A
win, settled five stake debts, and was corrected to a draw thirty-four seconds
later -- a draw pays nobody, so every row booked was wrong and had to be unwound
by hand.

Confirming before that write is only possible if the bot can answer "does this
selection end the draft?" BEFORE applying it. These cover the two pure pieces of
that answer, kept free of the database so the parity rule and the correction
swing can be pinned exactly.
"""
import pytest

from helpers.draft_outcome import decides_draft, standings_after


# --- decides_draft: the terminal condition ----------------------------------
# Extracted from utils.check_and_post_victory_or_draw so the confirmation and
# the settlement cannot disagree about what "final" means.

@pytest.mark.parametrize("a,b,total,expected", [
    (3, 2, 9, None),        # mid-draft, nothing settled
    (5, 4, 9, "team_a"),    # 5 of 9 is unreachable for B
    (4, 5, 9, "team_b"),
    (4, 4, 9, None),        # odd total CANNOT draw -- one match is still owed
    (4, 4, 8, "draw"),      # even total, split down the middle
    (5, 3, 8, "team_a"),
    (4, 2, 8, None),        # 4 of 8 is not yet a clinch; B can still reach 4
])
def test_decides_draft(a, b, total, expected):
    assert decides_draft(a, b, total) == expected


def test_a_draw_needs_every_match_played():
    """4-4 of 9 looks symmetric but the ninth match still decides it. Treating
    it as a draw would settle a pot while a match is outstanding."""
    assert decides_draft(4, 4, 9) is None


# --- standings_after: applying one report, including a correction -----------

def test_a_first_report_adds_a_win():
    assert standings_after(2, 3, previous_winner_side=None, new_winner_side="a") == (3, 3)


def test_correcting_the_winner_swings_two():
    """The case that produced the incident. Re-reporting a match from A to B is
    -1/+1, not +1: counting it as a fresh win overstates B and can make the
    check fire on a score that never existed."""
    assert standings_after(5, 3, previous_winner_side="a", new_winner_side="b") == (4, 4)


def test_correcting_to_no_match_played_removes_the_win():
    assert standings_after(5, 3, previous_winner_side="a", new_winner_side=None) == (4, 3)


def test_rereporting_the_same_winner_changes_nothing():
    """2-0 corrected to 2-1 keeps the same winner. Double-counting here would
    invent a clinch out of a cosmetic edit."""
    assert standings_after(4, 3, previous_winner_side="a", new_winner_side="a") == (4, 3)


def test_an_unreported_match_left_unreported_changes_nothing():
    assert standings_after(4, 3, previous_winner_side=None, new_winner_side=None) == (4, 3)


def test_a_correction_can_undo_a_clinch():
    """Together the two functions must be able to say 'this edit UN-decides the
    draft' -- the state icebind-pillar-36 was corrected into."""
    a, b = standings_after(5, 4, previous_winner_side="a", new_winner_side="b")
    assert (a, b) == (4, 5)
    assert decides_draft(a, b, 9) == "team_b"


def test_a_draft_with_no_matches_is_not_a_draw():
    """0-0 of nothing is undecided, not level.

    A draft sits at match_counter=1 (total_matches=0) from creation until its
    pairings exist. The draw clause -- both sides at half, total even -- is
    satisfied by 0 == 0 with 0 % 2 == 0, so a brand-new draft read as a DRAW.
    Anything acting on that decides a draft nobody has played: the prize pool
    refunds every entry at team creation, and the victory embed announces a
    result before the first pick.
    """
    assert decides_draft(0, 0, 0) is None


def test_a_draw_still_requires_every_match_played():
    """The guard above must not accidentally excuse a real draw."""
    assert decides_draft(1, 1, 2) == "draw"
    assert decides_draft(0, 0, 2) is None      # two matches owed, none played


def test_total_matches_reads_a_counter_that_was_never_set():
    """match_counter is the NEXT match number, so the count is one less.

    That subtraction was spelled two ways: the settlement path guarded against
    a null counter and the display path did not, so the same draft could settle
    cleanly and then crash the embed that reports it. One definition, and the
    guard is not optional.
    """
    from helpers.draft_outcome import total_matches_in

    assert total_matches_in(4) == 3
    assert total_matches_in(1) == 0
    assert total_matches_in(None) == 0
