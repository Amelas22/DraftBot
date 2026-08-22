"""Pure state and text for a tournament match's control message.

No Discord imports and no database access: the state table and every string
the control message can show are plain functions, so they are unit-testable
directly. All I/O around them lives in match_control_view.py.
"""

SCHEDULING = "scheduling"
DRAFTING = "drafting"
RECORDED = "recorded"


def match_state(has_result: bool, has_linked_draft: bool) -> str:
    """Which of the three states a match is in.

    ``has_result`` is checked first and that order is load-bearing: a linked
    draft row and a recorded result coexist for the whole window between a
    draft finishing and cleanup reaping it, so checking the draft first would
    render a finished match as still drafting.
    """
    if has_result:
        return RECORDED
    if has_linked_draft:
        return DRAFTING
    return SCHEDULING


def recorded_result_line(a_name: str, b_name: str, a_wins: int, b_wins: int) -> str:
    """The 'result recorded' line shown on a played match.

    Lives here rather than in the cog so the pairing message and the control
    message cannot render the same result two different ways.
    """
    return f"✅ Result recorded: **{a_name}** {a_wins}–{b_wins} **{b_name}**"


def render_match_control(
    state: str,
    a_name: str,
    b_name: str,
    round_number: int,
    lobby_link: str | None = None,
    result: tuple[int, int] | None = None,
) -> str:
    """Body text of the control message for a match in ``state``."""
    header = f"**Round {round_number} — {a_name} vs {b_name}**"
    if state == RECORDED:
        assert result is not None, "result required when state is RECORDED"
        a_wins, b_wins = result
        return f"{header}\n{recorded_result_line(a_name, b_name, a_wins, b_wins)}"
    if state == DRAFTING:
        if lobby_link:
            return f"{header}\n🟢 Draft in progress — [jump to the lobby]({lobby_link})"
        return f"{header}\n🟢 Draft in progress."
    return f"{header}\nNot started yet. Hit **Start draft** when both teams are ready."


def render_pairing_line(
    a_name: str,
    b_name: str,
    thread_id: str | None = None,
    result: tuple[int | None, int | None] | None = None,
) -> str:
    """One match's line on the pairings message.

    Carries a link to the match's room, and the score once the match is played,
    so the pairings channel reads as an index of the round. A match with no
    thread (Discord refused to create one) degrades to the names alone rather
    than rendering a broken mention.
    """
    line = f"• **{a_name}** vs **{b_name}**"
    if thread_id:
        line = f"{line} — <#{thread_id}>"
    if result is not None and result[0] is not None and result[1] is not None:
        return f"{line}\n{recorded_result_line(a_name, b_name, result[0], result[1])}"
    return line


def launch_block_text(
    state: str, lobby_link: str | None, recorded_line: str
) -> str | None:
    """Why a new draft can't start for a match in ``state``, or None.

    Shared by the Start draft button and by /premade_draft inside a match
    thread, so the two entry points cannot drift apart.
    """
    if state == RECORDED:
        return f"{recorded_line}\nAsk an admin if it needs correcting."
    if state == DRAFTING:
        if lobby_link:
            return f"A draft for this match is already underway — join it here: {lobby_link}"
        return "A draft for this match is already underway."
    return None
