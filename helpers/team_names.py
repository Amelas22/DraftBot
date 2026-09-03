"""What a draft's two sides are called, wherever a player can see them.

Every surface that shows a team used to decide this for itself, and the copies
disagreed. Three of them keyed on `session_type in ("random", "staked")` --
except utils.py, which also counted "test" -- and rendered "Team Red"; the rest
fell back to `team_a_name or "Team A"`. So one random draft's teams embed said
"Team Red" while the projected score under it said "Team A".

The rule here keys on the NAME rather than the session type, which is the fact
that actually decides it: a side either has a name somebody chose, or it does
not and gets its colour. That also catches the drafts where the placeholder was
written to the database -- premade signup used to store the literal "Team A"
when the creator left the field blank -- which no session-type test could see.

It is decided per side. A creator can fill one input and not the other, and
"Pack Rats vs Team Blue" is what they described; sending both back to colours
would throw away a name somebody typed.
"""
from typing import NamedTuple, Protocol


class TeamLabel(NamedTuple):
    """One side's name, and the colour dressing that goes with it.

    `emoji` and `color` are empty for a side that carries a chosen name: a
    named team is not a colour, and prefixing "Pack Rats" with a red circle
    reads as though it were one. They are separate fields rather than baked
    into `name` because callers need them apart -- `add_links_to_embed_safely`
    takes a bare "red"/"blue" token, and Draftmancer team names must not carry
    an emoji at all.
    """
    name: str
    emoji: str
    color: str

    @property
    def prefix(self) -> str:
        """The emoji and its separating space, or nothing for a named team."""
        return f"{self.emoji} " if self.emoji else ""

    @property
    def labelled(self) -> str:
        """The name as an embed field heading: "🔴 Team Red", or "Pack Rats".

        Use this wherever the label heads a block a reader scans -- an embed
        field, a standings line. Use `name` where the emoji would be noise or
        is added by something else: a button, a Draftmancer team name, a
        sentence the label appears inside, or a caller that takes `color` and
        renders the emoji itself.
        """
        return f"{self.prefix}{self.name}"


RED = TeamLabel("Team Red", "🔴", "red")
BLUE = TeamLabel("Team Blue", "🔵", "blue")

# The placeholders the old code both displayed and, on the premade path, wrote
# to the database. A row carrying one of these was never named by anyone, so it
# is treated as blank rather than as a choice.
_PLACEHOLDERS = {"team a", "team b", "team red", "team blue"}


def _chosen(name: str | None) -> str | None:
    """The name a person actually picked, or None if it is a placeholder."""
    if name is None:
        return None
    stripped = name.strip()
    if not stripped or stripped.lower() in _PLACEHOLDERS:
        return None
    return stripped


class HasTeamNames(Protocol):
    """Anything that carries the two stored names.

    Three unrelated classes do -- a DraftSession row, a PersistentView, a
    SessionDetails -- and they share no base. Stating the shape rather than
    naming a type is what lets one function serve all three without either
    importing the models or falling back to Any.
    """
    team_a_name: str | None
    team_b_name: str | None


def team_labels(team_a_name: str | None,
                team_b_name: str | None) -> tuple[TeamLabel, TeamLabel]:
    """The two labels to show for a draft, given whatever names it stored.

    Takes the two raw values rather than a session, so it stays a pure function
    over the only thing it depends on -- every caller already has them to hand,
    and it can be tested without a database.
    """
    chosen_a, chosen_b = _chosen(team_a_name), _chosen(team_b_name)
    return (RED._replace(name=chosen_a, emoji="", color="") if chosen_a else RED,
            BLUE._replace(name=chosen_b, emoji="", color="") if chosen_b else BLUE)


def labels_for(draft: HasTeamNames) -> tuple[TeamLabel, TeamLabel]:
    """The two labels for anything carrying team_a_name and team_b_name.

    Every caller but one has such an object to hand -- a DraftSession, a view,
    a SessionDetails -- and spelling the pair out at each of them invited the
    one mistake this cannot recover from: passing the names the wrong way
    round silently trades the colours over, and no test would fail because
    both orders produce valid labels. Naming the object instead of its two
    fields removes the chance.
    """
    return team_labels(draft.team_a_name, draft.team_b_name)
