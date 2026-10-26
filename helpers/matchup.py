"""Pure state and text for a team-draft matchup thread.

No Discord imports and no database access: a matchup is two sides, a booked
time and a thread's control message, all rendered from plain values so every
string is unit-testable directly. The I/O around it lives in the thread view.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

OPEN = "open"
STARTED = "started"

_THREAD_NAME_MAX = 100


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


def matchup_state(has_draft: bool) -> str:
    return STARTED if has_draft else OPEN


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
    if booking.start_utc is not None:
        when = f"Draft time: {discord_time(booking.start_utc)}"
        if booking.by_name:
            when += f" — booked by {booking.by_name}"
        lines.append(when)
    else:
        lines.append("No time booked yet — press **Find a time**.")
    if state == STARTED:
        if lobby_link:
            lines.append(f"🟢 Draft started — [jump to the lobby]({lobby_link})")
        else:
            lines.append("✅ Draft played.")
    return "\n".join(lines)


def launch_block(state: str, lobby_link: str | None) -> str | None:
    """Why a new draft can't start for a matchup in ``state``, or None."""
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
