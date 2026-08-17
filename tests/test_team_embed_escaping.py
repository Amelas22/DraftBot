"""Display names in the teams embeds, and where escaping them is correct.

Escaping is context-dependent, which is not obvious from the code:

  - PLAIN embed fields (team rosters, seating order): Discord consumes the
    backslashes, so an escaped name renders literally. Escape here.
  - MARKDOWN LINK LABELS: Discord does NOT process backslash escapes inside
    `[label](url)` -- the backslashes render as visible characters. Verified
    live in Discord, where escaped labels came out as "[TEST] \\*\\*Bold\\*\\*User".
    Leave raw here; the name renders with formatting applied, which is cosmetic
    because the URL carries the authoritative name and the Seating Order field
    is the one players read to match their Draftmancer seat.
"""
from types import SimpleNamespace

import pytest

from services import team_creator

MARKDOWN_NAMES = {
    "a1": "**Bold**User",
    "a2": "*Single*Star",
    "b1": "~Strike~User",
    "b2": "plain",
}


def fake_session():
    return SimpleNamespace(
        session_type="random",
        sign_ups=dict(MARKDOWN_NAMES),
        team_a=["a1", "a2"],
        team_b=["b1", "b2"],
        team_a_name=None,
        team_b_name=None,
        cube="LSVCube",
        friendly_id="test-1",
        get_draft_link_for_user=lambda name: f"https://draftmancer.com/?session=X&userName={name}",
    )


def field_values(embed):
    return "\n".join(f.value for f in embed.fields)


@pytest.mark.asyncio
async def test_draft_link_labels_are_left_raw():
    """Escaping a link label puts visible backslashes in front of players."""
    embed = await team_creator._create_channel_announcement_embed(
        fake_session(), ["**Bold**User"], {}, "random"
    )
    links = "\n".join(f.value for f in embed.fields if "Draft Links" in f.name)
    assert "[**Bold**User]" in links, f"link label should stay raw: {links}"
    assert "\\*\\*Bold\\*\\*User" not in links, "escaped label renders literal backslashes in Discord"


@pytest.mark.asyncio
async def test_link_urls_keep_the_raw_name():
    """The URL becomes ?userName= and is what seats the player -- escaping it
    would send backslashes to Draftmancer and break the join."""
    embed = await team_creator._create_channel_announcement_embed(
        fake_session(), ["**Bold**User"], {}, "random"
    )
    assert "userName=**Bold**User" in field_values(embed)


@pytest.mark.asyncio
async def test_announcement_seating_order_is_escaped():
    embed = await team_creator._create_channel_announcement_embed(
        fake_session(), ["**Bold**User", "*Single*Star"], {}, "random"
    )
    seating = next(f.value for f in embed.fields if f.name == "Seating Order")
    assert "\\*\\*Bold\\*\\*User" in seating, f"plain field should be escaped: {seating}"


@pytest.mark.asyncio
async def test_team_roster_lists_are_escaped():
    """Plain fields, so the escapes are consumed and the name renders literally."""
    embed = await team_creator._create_teams_embed(
        fake_session(), ["**Bold**User", "*Single*Star"], ["~Strike~User", "plain"],
        ["**Bold**User"], {}, "random",
    )
    roster = next(f.value for f in embed.fields if "Team Red" in f.name)
    assert "\\*\\*Bold\\*\\*User" in roster, f"roster should be escaped: {roster}"
