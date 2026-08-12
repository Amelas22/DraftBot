"""How a draft identifies itself in the posts it makes.

Embeds carry the universal metadata footer shared by every post tied to a
single draft; plain-text announcements, which have no footer to hang metadata
on, carry the inline reference from `draft_reference` instead.

Embed footers don't render markdown — bold, code spans, `-#` subtext and
`<t:...>` tags all show up as literal characters — so the footer text here is
plain by necessity. Inline references are ordinary message content and do take
markdown.

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


def draft_reference(draft_session: object) -> str:
    """Build an inline, code-spanned reference to a draft for plain-text posts.

    Signup channels host several drafts at once, so an unqualified "the draft"
    leaves readers guessing which one an announcement is about. This names it
    with the same `friendly_id` the embed footer shows.

    Returns "" when the session has no friendly_id — rows predating it, and
    stubs in tests — so callers fall back to their unqualified wording instead
    of announcing a draft called "None".
    """
    friendly_id = getattr(draft_session, "friendly_id", None)
    return f"`{friendly_id}`" if friendly_id else ""
