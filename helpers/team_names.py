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

    Every side has a colour; only the `name` depends on what was stored. They
    are separate fields rather than baked into `name` because callers need them
    apart: a button label and a Draftmancer team name take the bare `name`,
    while an embed heading takes `labelled`.
    """
    name: str
    emoji: str
    color: str

    @property
    def prefix(self) -> str:
        """The emoji and its separating space, or nothing for the shared chat.

        Every side of a draft has a colour. The one label without an emoji is
        the shared draft chat's, which is not a side.
        """
        return f"{self.emoji} " if self.emoji else ""

    @property
    def labelled(self) -> str:
        """The name as an embed field heading: "🔴 Team Red", "🔴 Pack Rats".

        Use this wherever the label heads a block a reader scans -- an embed
        field, a standings line. Use `name` where the emoji would be noise: a
        button, a Draftmancer team name, or a sentence the label appears
        inside.
        """
        # Idempotent for THIS side's emoji: captains typed "🔴 Pack Rats" by
        # hand while the heading carried no colour, and prepending again gives
        # "🔴 🔴 Pack Rats". Only this side's emoji is absorbed -- a name
        # starting with the other side's keeps both, because the heading must
        # still say which side it is.
        if self.emoji and self.name.startswith(self.emoji):
            return self.name
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
    def label(chosen: str | None, colour: TeamLabel) -> TeamLabel:
        # The NAME is what a stored value can change. The colour is not: the
        # side's rooms are red-team-chat-*, and Draftmancer puts a red dot
        # beside its players, whether or not anybody named it. A label that
        # reported no colour could only disagree with those.
        return colour._replace(name=chosen) if chosen else colour

    return (label(_chosen(team_a_name), RED),
            label(_chosen(team_b_name), BLUE))


def heads_field(label: TeamLabel, field_name: str) -> bool:
    """Whether an existing embed field is this side's, by its heading.

    Either spelling counts, because the heading was written by whichever
    version of the bot created the draft. A named premade used to be headed
    "Pack Rats" and is now headed "\U0001f534 Pack Rats"; a draft already in
    flight when that shipped carries the old one, and matching only the new one
    would leave its join buttons silently updating nothing.

    The match is BOUNDED: a team's heading is the label exactly, as written at
    creation, or the label followed by its count suffix once somebody has
    joined. An open-ended prefix match is not enough, because the signup embed
    puts "Cube:" and "Pack Format:" ahead of the team fields -- a team called
    "Pack" matched "Pack Format:" first, and the join then overwrote the
    draft's pack format with a roster.
    """
    return any(field_name == spelling or field_name.startswith(f"{spelling} (")
               for spelling in (label.labelled, label.name))


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
