"""One answer to "what are this draft's two sides called".

The rule used to be inlined at every surface that showed a team, and each copy
tested something slightly different: utils.py counted a "test" draft as
anonymous and team_creator.py did not, so the same draft could render as
"Team Red" in one embed and "Team A" in the next. These pin the single rule.
"""
import pytest

from helpers.team_names import RED, BLUE, team_labels


def test_a_draft_with_no_team_names_is_red_and_blue():
    """The ordinary random or staked draft: nobody named the sides."""
    red, blue = team_labels(None, None)

    assert (red.name, blue.name) == ("Team Red", "Team Blue")
    assert (red.emoji, blue.emoji) == ("🔴", "🔵")
    assert (red.color, blue.color) == ("red", "blue")


def test_real_premade_names_are_left_alone():
    """380 prod drafts carry names their captains chose. Colouring them would
    be renaming somebody's team."""
    red, blue = team_labels("Pack Rats", "PDX Pandas")

    assert (red.name, blue.name) == ("Pack Rats", "PDX Pandas")
    assert (red.emoji, blue.emoji) == ("", ""), (
        "a named team is not a colour, and an emoji in front of it reads as one")
    assert (red.color, blue.color) == ("", "")


@pytest.mark.parametrize("stored", ["Team A", "team a", "  Team A  ", "", "   "])
def test_a_stored_placeholder_is_not_a_name(stored):
    """premade_session used to write the literal "Team A" whenever the creator
    left the field blank, so the placeholder is in the database on real rows.
    Reading it back as a chosen name is what kept those drafts saying "Team A"
    while every other surface said "Team Red".
    """
    red, _ = team_labels(stored, "Team B")

    assert red.name == "Team Red", (
        f"the stored placeholder {stored!r} was treated as a chosen name")


def test_each_side_is_decided_on_its_own_name():
    """Half-filled is a real state -- the modal takes two inputs and validates
    neither, so a creator can name one side and leave the other blank.

    The rule is per-side rather than all-or-nothing: the named side keeps its
    name, the blank one gets its colour. "Pack Rats vs Team Blue" is what the
    creator actually described; forcing both back to colours would discard a
    name somebody typed, and forcing both to be names has none to use.
    """
    red, blue = team_labels("Pack Rats", None)

    assert (red.name, blue.name) == ("Pack Rats", "Team Blue")
    assert (red.emoji, blue.emoji) == ("", "🔵")


def test_the_labels_are_the_constants_the_module_exports():
    """Callers that need to compare against a label -- matching an embed field
    by name, say -- must not spell it out a second time."""
    red, blue = team_labels(None, None)

    assert red.name == RED.name and blue.name == BLUE.name


def test_no_surface_spells_a_team_label_for_itself():
    """The rule has to live in one place to stay consistent.

    Before this helper, six surfaces each decided for themselves whether a
    draft's sides were coloured, and they used three different conditions:
    utils.py counted a "test" draft as anonymous, team_creator.py did not, and
    livedrafts.py coloured every draft including premades with real names. The
    visible result was one random draft whose teams embed said "Team Red" and
    whose projected score, directly beneath it, said "Team A".

    Nothing about that was catchable by testing any one surface, so this checks
    the property that actually failed: the strings exist in exactly one module.
    """
    import re
    from pathlib import Path

    label = re.compile(r'["\']\s*(?:🔴|🔵)?\s*Team (?:A|B|Red|Blue)\b')

    def is_player_facing(line):
        """Comments and log lines are exempt, and honestly so.

        A comment is prose about the code, and a log line names the code-side
        A/B split -- session.team_a, the JSON column -- which this change
        deliberately leaves alone. Neither reaches a player, and forcing them
        through the helper would make the log harder to read, not the UI more
        consistent.
        """
        stripped = line.strip()
        return not stripped.startswith("#") and "logger." not in line

    offenders = {}
    for path in ("utils.py", "views.py", "livedrafts.py", "modals.py",
                 "services/team_creator.py", "sessions/premade_session.py",
                 "helpers/test_users.py"):
        hits = [
            f"{path}:{n}"
            for n, line in enumerate(Path(path).read_text().split("\n"), 1)
            if label.search(line) and is_player_facing(line)
        ]
        if hits:
            offenders[path] = hits

    assert not offenders, (
        "a team label is spelled out outside helpers/team_names.py; call "
        f"team_labels() instead so every surface agrees: {offenders}")
