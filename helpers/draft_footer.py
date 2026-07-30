"""Universal metadata footer shared by every post tied to a single draft.

Embed footers don't render markdown — bold, code spans, `-#` subtext and
`<t:...>` tags all show up as literal characters — so the text here is plain by
necessity.

Drafts are identified by their `friendly_id` (a stable, human-readable id like
"lightning-bolt-7", generated once at session creation) rather than the
Draftmancer `draft_id` (an 8-char code that can be regenerated mid-draft on
reconnect) or the full `session_id` (`{user_id}-{unix_ts}`, long and embeds
the initiating user's Discord ID).
"""

SEPARATOR = " • "


def draft_footer_text(friendly_id, cube):
    """Build the plain-text footer label for a draft.

    Missing pieces drop their labels rather than rendering as "None"; returns ""
    when nothing is available so callers can skip the footer entirely.
    """
    parts = []
    if friendly_id:
        parts.append(f"ID: {friendly_id}")
    if cube:
        parts.append(f"Cube: {cube}")
    return SEPARATOR.join(parts)


def apply_draft_footer(embed, friendly_id, cube):
    """Stamp a draft's metadata onto an embed's footer."""
    text = draft_footer_text(friendly_id, cube)
    if text:
        embed.set_footer(text=text)
    return embed


def apply_draft_footer_from_session(embed, draft_session):
    """Stamp draft metadata onto an embed, reading from a DraftSession row."""
    return apply_draft_footer(embed, draft_session.friendly_id, draft_session.cube)
