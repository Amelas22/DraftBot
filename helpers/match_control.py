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


def match_tag(match_id: int, name: str) -> str:
    """"#143 · Quarterfinal": a match's id and round name, as every surface shows them."""
    return f"#{match_id} · {name}"


def render_match_control(
    state: str,
    a_name: str,
    b_name: str,
    match_label: str,
    lobby_link: str | None = None,
    result: tuple[int, int] | None = None,
    role_mentions: tuple[str | None, str | None] | None = None,
) -> str:
    """Body text of the control message for a match in ``state``.

    ``match_label`` is match_tag(id, round name), the name rendered by
    tournament_formatter.round_name -- this module stays free of the database
    a bracket round's name has to be read from.

    ``role_mentions`` is (team_a's role id, team_b's role id), and only
    prepends a mention line when BOTH are present -- tagging one team and not
    the other is worse than tagging neither, since nothing on the message
    would explain the gap.
    """
    header = f"**{match_label} — {a_name} vs {b_name}**"
    if state == RECORDED:
        assert result is not None, "result required when state is RECORDED"
        a_wins, b_wins = result
        body = f"{header}\n{recorded_result_line(a_name, b_name, a_wins, b_wins)}"
    elif state == DRAFTING:
        if lobby_link:
            body = f"{header}\n🟢 Draft in progress — [jump to the lobby]({lobby_link})"
        else:
            body = f"{header}\n🟢 Draft in progress."
    else:
        body = f"{header}\nNot started yet. Hit **Start draft** when both teams are ready."
    if role_mentions and all(role_mentions):
        body = f"<@&{role_mentions[0]}> <@&{role_mentions[1]}>\n" + body
    return body


def render_pairing_line(
    a_name: str,
    b_name: str,
    thread_id: str | None = None,
    result: tuple[int | None, int | None] | None = None,
    match_id: int | None = None,
    stage: str | None = None,
) -> str:
    """One match's line on the pairings message.

    ``match_id`` and ``stage`` (the round's name) lead the line so the id an
    admin needs for /tournament set_result is on screen.

    Carries a link to the match's room, and the score once the match is played,
    so the pairings channel reads as an index of the round. A match with no
    thread (Discord refused to create one) degrades to the names alone rather
    than rendering a broken mention.
    """
    line = f"• **{a_name}** vs **{b_name}**"
    if match_id is not None and stage:
        line = f"• **#{match_id}** · {stage} · **{a_name}** vs **{b_name}**"
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
