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


def _production_sources():
    """Every module the bot actually runs, as (path, text).

    Scanning a hand-written list of files is what let this rule rot in the
    first place: /add_sub offered a dropdown of "A" and "B" for months because
    cogs/ was not on anybody's list. Tests and scripts are excluded because
    they are not surfaces a player sees; .claude/worktrees because those are
    other checkouts of this same repo.
    """
    from pathlib import Path

    skip = ("tests/", "scripts/", ".claude/", "alembic/", "backfill_")
    for path in sorted(Path(".").rglob("*.py")):
        rel = path.as_posix()
        if any(rel.startswith(s) or f"/{s}" in rel for s in skip):
            continue
        if rel == "helpers/team_names.py":
            continue
        yield rel, path.read_text()


def _display_strings(path, text):
    """Every string literal in a module that a player could actually read.

    Parsed rather than grepped, because the line-based version could not tell a
    label from prose about one: a trailing comment reading `# premade team
    name` and a docstring reading "Team A Discord IDs from DB" both matched,
    and both are documentation of the JSON column this change deliberately
    keeps. The parser sees no comments at all, and a docstring is the one
    string form that is never rendered to anybody.

    Log calls are dropped for the same reason: a log naming team_a means the
    column, and routing it through the helper would make the log worse rather
    than the UI better.
    """
    import ast

    tree = ast.parse(text, filename=path)
    exempt = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                exempt.add(id(body[0].value))
        if isinstance(node, ast.Call):
            func = node.func
            # Any *logger, because modules bind their own: stake_calculator
            # logs through `stake_logger`, and a name-list would miss it.
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and (func.value.id.endswith("logger")
                         or func.value.id in ("logging", "log"))):
                exempt.update(id(n) for n in ast.walk(node))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in exempt):
            yield node.lineno, node.value


def test_no_surface_spells_a_team_label_for_itself():
    """The rule has to live in one place to stay consistent.

    Before this helper, each surface decided for itself whether a draft's sides
    were coloured, using three different conditions AND three different
    spellings -- "Team A", "Team Red" and "Red Team" all shipped at once. One
    random draft's teams embed said "Team Red" while the projected score
    beneath it said "Team A", and the sub-grant confirmation said "Red Team".

    No single-surface test could catch that, so this checks the property that
    actually failed: the strings exist in exactly one module.
    """
    import re

    label = re.compile(
        r'["\']\s*(?:🔴|🔵)?\s*(?:Team (?:A|B|Red|Blue)|(?:Red|Blue) Team)\b')

    offenders = {}
    for path, text in _production_sources():
        hits = [f"{path}:{lineno} {value!r}"
                for lineno, value in _display_strings(path, text)
                if label.search(chr(34) + value + chr(34))]
        if hits:
            offenders[path] = hits

    assert not offenders, (
        "a team label is spelled out outside helpers/team_names.py; call "
        f"team_labels() instead so every surface agrees: {offenders}")


def test_no_user_facing_message_reads_a_stored_team_name_raw():
    """The class of bug the string checks above cannot see.

    team_a_name is NULL for a draft nobody named, so interpolating it straight
    into a message prints "None". Two did: the test-user fill said "Added 6
    test users. None: 3/3, None: 3/3", and -- in production -- an entry-fee
    draft with uneven sides refused with "None has 3 players and None has 2".

    Neither contained a team label as a literal, so nothing that greps for one
    could find them. What they have in common is reading the stored field
    inside a call that renders to a person, which is what this looks for.
    """
    import ast

    UI = {"send", "send_message", "followup", "respond", "edit_message",
          "add_field", "set_author", "set_footer", "Embed", "InputText",
          "OptionChoice", "Option", "Button", "SelectOption", "add_item",
          "set_field_at", "edit", "_add_button"}

    def is_log(node):
        f = getattr(node, "func", None)
        return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and (f.value.id.endswith("logger")
                     or f.value.id in ("logging", "log", "print")))

    offenders = []
    for path, text in _production_sources():
        for node in ast.walk(ast.parse(text, filename=path)):
            if not isinstance(node, ast.Call) or is_log(node):
                continue
            func = node.func
            named = (func.attr if isinstance(func, ast.Attribute)
                     else getattr(func, "id", ""))
            if named not in UI:
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Attribute)
                        and inner.attr in ("team_a_name", "team_b_name")):
                    offenders.append(f"{path}:{inner.lineno} .{inner.attr}")

    assert not offenders, (
        "a stored team name is rendered to a player without going through "
        f"team_labels, so an unnamed draft will show \"None\": {offenders}")


def test_no_command_asks_a_player_to_pick_a_letter():
    """/add_sub took `choices=["A", "B"]`, so the dropdown a player opened
    showed two bare letters -- the one surface where the internal A/B split
    leaked all the way out to a user picking from it.

    The stored VALUES stay "A"/"B", because resolve_sub_grant keys on them and
    a slash-command choice value is not persisted anywhere. Only the name a
    player reads had to change, which is why this checks the names.
    """
    import re

    bare = re.compile(r'choices\s*=\s*\[\s*["\'][AB]["\']')
    offenders = [f"{path}:{n}" for path, text in _production_sources()
                 for n, line in enumerate(text.split("\n"), 1)
                 if bare.search(line)]

    assert not offenders, (
        f"a slash command offers a bare team letter as a choice: {offenders}")
