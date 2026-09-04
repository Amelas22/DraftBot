"""One answer to "what are this draft's two sides called".

The rule used to be inlined at every surface that showed a team, and each copy
tested something slightly different: utils.py counted a "test" draft as
anonymous and team_creator.py did not, so the same draft could render as
"Team Red" in one embed and "Team A" in the next. These pin the single rule.
"""
import ast
import functools
from pathlib import Path

import pytest

from helpers.team_names import RED, BLUE, heads_field, team_labels


def test_a_draft_with_no_team_names_is_red_and_blue():
    """The ordinary random or staked draft: nobody named the sides."""
    red, blue = team_labels(None, None)

    assert (red.name, blue.name) == ("Team Red", "Team Blue")
    assert (red.emoji, blue.emoji) == ("🔴", "🔵")
    assert (red.color, blue.color) == ("red", "blue")


def test_a_named_team_keeps_its_name_and_its_side_s_colour():
    """380 prod drafts carry names their captains chose. Those names win -- but
    the colour is not a label the draft can opt out of.

    A named side is still seated in red-team-chat and still shows a red dot
    beside its players on Draftmancer. Dropping the emoji from the one surface
    that NAMES the team meant a Pack Rats player sat in a red room with a red
    dot and was never told which colour they were: the association was left
    unstated rather than absent, which is worse than either.
    """
    red, blue = team_labels("Pack Rats", "PDX Pandas")

    assert (red.name, blue.name) == ("Pack Rats", "PDX Pandas"), (
        "a captain's chosen name is the identity and must survive intact")
    assert (red.emoji, blue.emoji) == ("🔴", "🔵")
    assert (red.color, blue.color) == ("red", "blue")
    assert red.labelled == "🔴 Pack Rats"


def test_every_side_carries_a_colour_whatever_it_is_called():
    """The invariant the surfaces depend on: the room, the Draftmancer dot and
    the embed heading all colour a side, so a label that reports no colour can
    only disagree with them."""
    for a, b in ((None, None), ("Pack Rats", "PDX Pandas"), ("Pack Rats", None)):
        red, blue = team_labels(a, b)
        assert red.emoji and red.color, f"red side lost its colour for {(a, b)}"
        assert blue.emoji and blue.color, f"blue side lost its colour for {(a, b)}"
        assert (red.color, blue.color) == ("red", "blue")


def test_a_draft_already_in_flight_keeps_updating_its_signup_embed():
    """The deploy hazard this change creates.

    A named premade's signup field used to be headed "Pack Rats" and is now
    headed "\U0001f534 Pack Rats". A draft created before this shipped carries
    the old heading, so matching only the new one would find no field -- the
    join buttons would go on working while the embed silently stopped changing.
    """
    red, blue = team_labels("Pack Rats", "PDX Pandas")

    # written by the version that shipped before this change
    assert heads_field(red, "Pack Rats (3):")
    assert heads_field(blue, "PDX Pandas (3):")
    # and by this one
    assert heads_field(red, "\U0001f534 Pack Rats (3):")
    assert heads_field(blue, "\U0001f535 PDX Pandas (3):")

    # an unnamed draft was already emoji'd before this change, and still matches
    r, b = team_labels(None, None)
    assert heads_field(r, "\U0001f534 Team Red (3):")
    assert heads_field(b, "\U0001f535 Team Blue (3):")


def test_one_team_cannot_claim_the_other_teams_field():
    """The match is anchored at the start of the heading, so a name that
    contains the other name -- "Rats" against "Pack Rats" -- cannot cross."""
    short, long = team_labels("Rats", "Pack Rats")

    assert heads_field(short, "Rats (3):")
    assert not heads_field(short, "Pack Rats (3):")
    assert heads_field(long, "Pack Rats (3):")
    assert not heads_field(long, "Rats (3):")


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
    assert (red.emoji, blue.emoji) == ("🔴", "🔵"), (
        "both sides carry their colour; only the NAME depends on what was stored")


@functools.lru_cache(maxsize=1)
def _production_sources():
    """Every module the bot actually runs, as (path, text, parsed tree).

    The file set comes from `git ls-files`, not a walk plus a skip list. The
    repo already defines what it tracks, so ignored checkouts -- including the
    worktrees under .claude/, which are whole copies of this same tree -- drop
    out for free rather than needing to be named. It is also anchored at the
    repo root instead of the process CWD, which a walk was not: run from
    anywhere else, a walk finds nothing and every rule below passes vacuously.

    Parsed once and cached, because three tests read this and parsing the tree
    is the expensive part.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "*.py"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert listed, "git ls-files returned nothing -- the rules below would pass vacuously"

    skip = ("tests/", "scripts/", "alembic/", "backfill_")
    out = []
    for rel in sorted(listed):
        if rel.startswith(skip) or rel == "helpers/team_names.py":
            continue
        text = (root / rel).read_text()
        try:
            out.append((rel, text, ast.parse(text, filename=rel)))
        except SyntaxError:
            continue
    return tuple(out)


def _display_strings(path, tree):
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
    for path, text, tree in _production_sources():
        hits = [f"{path}:{lineno} {value!r}"
                for lineno, value in _display_strings(path, tree)
                if label.search(chr(34) + value + chr(34))]
        if hits:
            offenders[path] = hits

    assert not offenders, (
        "a team label is spelled out outside helpers/team_names.py; call "
        f"team_labels() instead so every surface agrees: {offenders}")


def test_no_stored_team_name_is_interpolated_into_a_message():
    """The class of bug the string checks above cannot see -- and the reason
    this rule is about TAINT rather than about which call it sits in.

    team_a_name is NULL for a draft nobody named, so putting it in a message
    prints "None". The first version of this guard looked for the attribute
    inside a call whose function name was in a hand-kept list of py-cord
    surfaces, and it passed a branch that shipped three of them, because they
    bind the value to a local first:

        team_name = draft_session.team_a_name if ... else ...
        title = f"{team_name} has won the match!"

    So the rule follows the value instead. A read may be plumbed anywhere --
    stored on a view, passed as a keyword argument, persisted, matched against
    a tournament roster -- but the moment it reaches an f-string it is being
    shown to somebody, and that has to come from team_labels.
    """
    import ast

    def tainted_names(fn):
        """Locals in `fn` assigned from a raw team-name read, transitively."""
        tainted, changed = set(), True
        while changed:
            changed = False
            for node in ast.walk(fn):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                # A raw read that is merely an ARGUMENT does not taint the
                # result: plan_premade_test_users(..., draft_session.team_a_name)
                # returns rosters, not names, and treating its rosters as
                # tainted flagged `len(team_a)` in an f-string. Taint follows
                # the value itself -- an attribute read, or a ternary or tuple
                # of them -- not everything a call was told.
                consumed = {id(n) for call in ast.walk(value)
                            if isinstance(call, ast.Call)
                            for n in ast.walk(call)}
                reads_raw = any(
                    ((isinstance(n, ast.Attribute)
                      and n.attr in ("team_a_name", "team_b_name"))
                     or (isinstance(n, ast.Name) and n.id in tainted))
                    and id(n) not in consumed
                    for n in ast.walk(value))
                if not reads_raw:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id not in tainted:
                            tainted.add(n.id)
                            changed = True
        return tainted

    def logged(fn):
        """Every node inside a log call -- a log naming the column is fine."""
        out = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        and (f.value.id.endswith("logger")
                             or f.value.id in ("logging", "log", "print"))):
                    out.update(id(n) for n in ast.walk(node))
        return out

    offenders = []
    for path, _text, tree in _production_sources():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            tainted, exempt = tainted_names(fn), logged(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.JoinedStr) or id(node) in exempt:
                    continue
                for inner in ast.walk(node):
                    raw = (isinstance(inner, ast.Attribute)
                           and inner.attr in ("team_a_name", "team_b_name"))
                    via = isinstance(inner, ast.Name) and inner.id in tainted
                    if raw or via:
                        offenders.append(f"{path}:{node.lineno} in {fn.name}()")
                        break

    assert not offenders, (
        "a stored team name reaches a message without going through "
        f"team_labels, so an unnamed draft will show \"None\": {sorted(set(offenders))}")


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
    offenders = [f"{path}:{n}" for path, text, _tree in _production_sources()
                 for n, line in enumerate(text.split("\n"), 1)
                 if bare.search(line)]

    assert not offenders, (
        f"a slash command offers a bare team letter as a choice: {offenders}")
