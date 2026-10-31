"""Pure state and text for a team-draft matchup thread.

No Discord imports and no database access: a matchup is two sides, a booked
time and a thread's control message, all rendered from plain values so every
string is unit-testable directly. The I/O around it lives in the thread view.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

OPEN = "open"
# Claimed, but the draft's row or its lobby doesn't exist yet: the seconds
# between Start draft winning the claim and the sign-up being posted.
SETTING_UP = "setting_up"
STARTED = "started"

_THREAD_NAME_MAX = 100

# The one answer every team-draft entry point gives in a server without the
# scheduler (board, wizard, /team_draft_board, Start draft, /premade_draft).
NOT_ENABLED = "Team drafts aren't turned on in this server."
# The same refusal for a tournament match room's Find a time button.
MATCH_SCHEDULING_NOT_ENABLED = "Find a time isn't turned on in this server."
# Shared by both Find a time buttons (team-draft matchups and tournament matches).
TIME_LINK_INTRO = ("See both sides' usual weeks side by side and book a time that suits "
                   "everyone. The link works for 7 days.")
TIME_LINK_FAILED = "Couldn't make your link just now. Try again in a minute."


@dataclass(frozen=True)
class Side:
    label: str
    role_ids: tuple[str, ...]
    user_ids: tuple[str, ...]

    def mentions(self) -> str:
        """Role mentions then user mentions, space-joined."""
        return " ".join(
            [f"<@&{r}>" for r in self.role_ids] + [f"<@{u}>" for u in self.user_ids]
        )


@dataclass(frozen=True)
class Roster:
    """A side as the scheduler page lists it: its label and its people,
    each a (user id, display name) pair."""

    label: str
    people: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Booking:
    start_utc: datetime | None
    by_name: str | None


def side_label(role_names: list[str], user_names: list[str]) -> str:
    """A side's display name: its role's name alone, else every name listed.

    The caller passes "deleted role" for a role Discord no longer has.
    """
    if len(role_names) == 1 and not user_names:
        return role_names[0]
    return ", ".join([*role_names, *user_names])


def matchup_name(a: Side, b: Side) -> str:
    """The thread name, cut to Discord's 100-character limit."""
    return f"{a.label} vs {b.label}"[:_THREAD_NAME_MAX]


def discord_time(start_utc: datetime) -> str:
    """A Discord timestamp pair (full date and relative); naive input is UTC."""
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    ts = int(start_utc.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)"


def booking_line(booking: Booking) -> str:
    """The control message's time line: the booked time, or a nudge to book one."""
    if booking.start_utc is None:
        return "No time booked yet — press **Find a time**."
    when = f"Draft time: {discord_time(booking.start_utc)}"
    if booking.by_name:
        when += f" — booked by {booking.by_name}"
    return when


def matchup_state(has_draft: bool) -> str:
    return STARTED if has_draft else OPEN


def claimed_state(has_lobby: bool, ran: bool) -> str:
    """The state of a matchup a draft has claimed: STARTED once that draft has a
    lobby or has run, SETTING_UP until then."""
    return STARTED if has_lobby or ran else SETTING_UP


def render_matchup_control(
    a: Side,
    b: Side,
    booking: Booking,
    state: str,
    set_up_by: str,
    lobby_link: str | None,
) -> str:
    """Body text of the thread's control message."""
    lines = [
        f"**{a.label} vs {b.label}**",
        f"{a.mentions()} vs {b.mentions()}",
        f"Set up by {set_up_by}.",
    ]
    lines.append(booking_line(booking))
    if state == SETTING_UP:
        lines.append("⏳ Draft being set up…")
    elif state == STARTED:
        if lobby_link:
            lines.append(f"🟢 Draft started — [jump to the lobby]({lobby_link})")
        else:
            lines.append("✅ Draft played.")
    return "\n".join(lines)


def launch_block(state: str, lobby_link: str | None) -> str | None:
    """Why a new draft can't start for a matchup in ``state``, or None."""
    if state == SETTING_UP:
        return "This matchup's draft is being set up right now — give it a moment."
    if state != STARTED:
        return None
    if lobby_link:
        return f"This matchup's draft has already started — join it here: {lobby_link}"
    return "This matchup's draft has already been played. Set up a new team draft for a rematch."


def booking_announcement(
    who: str, old: datetime | None, new: datetime | None, a: Side, b: Side
) -> str:
    """The post that tags both sides when a time is booked, moved or cleared."""
    if new is None:
        what = f"**{who}** cleared the booked time."
    elif old is None:
        what = f"**{who}** booked the draft for {discord_time(new)}."
    else:
        what = (
            f"**{who}** moved the draft from {discord_time(old)} to {discord_time(new)}."
        )
    return f"{a.mentions()} {b.mentions()}\n{what}"


def assign_people(
    a_named: list[str],
    a_role_members: list[str],
    b_named: list[str],
    b_role_members: list[str],
) -> tuple[list[str], list[str]]:
    """Split people between two sides, each person once, order kept.

    Someone named directly on a side stays there; a role member claimed by
    both sides (or named on the other) goes by that rule, else side A wins.
    """
    a_direct, b_direct = set(a_named), set(b_named)
    a_out: list[str] = []
    b_out: list[str] = []
    seen: set[str] = set()
    for person in [*a_named, *a_role_members]:
        if person in seen or (person in b_direct and person not in a_direct):
            continue
        seen.add(person)
        a_out.append(person)
    for person in [*b_named, *b_role_members]:
        if person in seen:
            continue
        seen.add(person)
        b_out.append(person)
    return a_out, b_out
