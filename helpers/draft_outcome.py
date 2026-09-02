"""Is the draft decided, and would this report decide it?

Both questions are answered here, without touching the database, because two
callers have to agree on the answer forever:

* `utils.check_and_post_victory_or_draw` asks it AFTER a report lands, to decide
  whether to post the victory embed and settle stakes.
* `views.MatchResultSelect` asks it BEFORE a report lands, to decide whether the
  reporter should be made to confirm.

If those two ever disagree the confirmation is worse than useless -- it either
guards reports that settle nothing, or waves through the one report that does.
So the rule lives in one place and neither caller restates it.
"""
from typing import Literal, Optional

Side = Optional[Literal["a", "b"]]
Outcome = Optional[Literal["team_a", "team_b", "draw"]]


def decides_draft(team_a_wins: int, team_b_wins: int, total_matches: int) -> Outcome:
    """Which way the draft is settled at this score, or None if it is still live.

    A side clinches by taking MORE than half the matches -- with nine matches
    that is five, because the other side can then reach at most four.

    A draw needs both halves exactly, and only when the total is even -- and
    only when there are matches at all. The parity clause is not decoration:
    4-4 of NINE is not a draw, it is a draft
    with one match still owed, and treating it as final would settle a pot
    before the deciding match is played.
    """
    if total_matches <= 0:
        # A draft with no matches is undecided, not level. Without this the draw
        # clause below is satisfied by 0 == 0 with 0 % 2 == 0, so a draft nobody
        # has played reads as a DRAW -- which refunds its prize pool at team
        # creation and announces a result before the first pick.
        return None

    half = total_matches // 2
    if team_a_wins > half:
        return "team_a"
    if team_b_wins > half:
        return "team_b"
    if team_a_wins == half and team_b_wins == half and total_matches % 2 == 0:
        return "draw"
    return None


def standings_after(
    team_a_wins: int,
    team_b_wins: int,
    *,
    previous_winner_side: Side,
    new_winner_side: Side,
) -> tuple[int, int]:
    """The score once ONE match's winner is changed from one side to another.

    Reporting is not append-only: a match already reported can be re-reported,
    and the common repair is flipping the winner. That is a swing of two -- the
    old side loses a win as the new side gains one -- so a caller that simply
    adds the new win overstates the score and can see a clinch that never
    happened. Re-reporting the same winner (2-0 corrected to 2-1) moves nothing.

    `None` on either side means the match is unreported, or was reported as no
    match played.
    """
    if previous_winner_side == new_winner_side:
        return team_a_wins, team_b_wins
    if previous_winner_side == "a":
        team_a_wins -= 1
    elif previous_winner_side == "b":
        team_b_wins -= 1
    if new_winner_side == "a":
        team_a_wins += 1
    elif new_winner_side == "b":
        team_b_wins += 1
    return team_a_wins, team_b_wins


def total_matches_in(match_counter: Optional[int]) -> int:
    """How many matches this draft has, from the counter that names the next one.

    Both the money and the embed that reports it derive the outcome from this
    number, and they used to spell the subtraction differently -- one guarding
    against a counter that was never set and one not. A draft that settled
    correctly could still crash the summary that announced it.
    """
    return max((match_counter or 1) - 1, 0)
