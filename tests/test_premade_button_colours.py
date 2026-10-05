"""Button colours in a draft view mean something, and no two buttons claim the same thing.

Red and blurple are reserved: they mark the side you join (premade) or the main thing you
came to do (Sign Up, everywhere else). Green is a safe useful action, grey is one you
should rarely need.

Built from a REAL PersistentView and read off `view.children`, which is the only way to
see the whole rendered set: `_add_shared_buttons` and the Ready Check / Create Rooms pair
land in the same view as the type-specific buttons, and `BetCapToggleButton` arrives
through `add_item` rather than `_add_button`. A test that drives `_add_premade_buttons`
alone cannot see any of them -- which is how a blurple Team Blue once shipped alongside a
blurple Update Cube and a blurple Create Rooms.
"""
import discord
import pytest

from views import PersistentView, TEAM_A_STYLE, TEAM_B_STYLE


SESSION_TYPES = ("premade", "random", "staked", "winston", "swiss", "test", "schedule")


def _buttons(session_type="premade", **names):
    """{custom_id suffix: child} for a real view of this type."""
    view = PersistentView(bot=None, draft_session_id="s1",
                          session_type=session_type, **names)
    return {(c.custom_id or "").removesuffix("_s1"): c
            for c in view.children if hasattr(c, "style")}


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type", SESSION_TYPES)
async def test_at_most_one_button_wears_each_reserved_colour(session_type):
    """The actual rule, rather than a list of today's answers.

    Red and blurple each carry one meaning per view, so a second button in either
    colour is the bug -- whichever session type, helper or future button introduces
    it. This is what the per-button pins below could not express: they would all
    pass while a newly-added blurple button sat next to Team Blue.
    """
    styles = [c.style for c in _buttons(session_type).values()]
    for reserved in (discord.ButtonStyle.danger, discord.ButtonStyle.primary):
        wearing = [s for s in styles if s is reserved]
        assert len(wearing) <= 1, (
            f"{session_type}: {len(wearing)} buttons wear {reserved.name}; it is reserved "
            f"for one meaning per view")


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, style", [
    # The side you join: from TEAM_A_STYLE / TEAM_B_STYLE, shared with the pairing grid.
    ("Team_A",                TEAM_A_STYLE),
    ("Team_B",                TEAM_B_STYLE),
    # Safe and useful.
    ("update_cube",           discord.ButtonStyle.success),
    ("ready_check",           discord.ButtonStyle.success),
    # Driven automatically; a human pressing one is the exception, hence grey rather
    # than a colour that reads as "press me next".
    ("generate_seating",      discord.ButtonStyle.secondary),
    ("create_rooms_pairings", discord.ButtonStyle.secondary),
    # Destructive, and sits with the rarely-needed rather than with green.
    ("cancel_draft",          discord.ButtonStyle.secondary),
    ("remove_user",           discord.ButtonStyle.secondary),
])
async def test_premade_button_colours(suffix, style):
    assert _buttons()[suffix].style is style


@pytest.mark.asyncio
async def test_team_buttons_keep_their_sides_colour_when_named():
    """Naming a team changes its label, never which side it is."""
    buttons = _buttons(team_a_name="Pack Rats", team_b_name="Mox Boys")
    assert buttons["Team_A"].label == "Pack Rats"
    assert buttons["Team_B"].label == "Mox Boys"
    assert buttons["Team_A"].style is TEAM_A_STYLE
    assert buttons["Team_B"].style is TEAM_B_STYLE


@pytest.mark.asyncio
async def test_sign_up_takes_blurple_only_where_there_is_no_team_to_join():
    """Blurple means "the main thing you came to do", which differs by session type.

    On an open draft that is Sign Up; on a premade it is picking a side. The two must
    never co-occur, and `add_buttons` is what guarantees it -- the signup pair is added
    only when session_type != "premade". Asserted here rather than left to the
    invariant above so a failure says which of the two assumptions broke.
    """
    assert _buttons("random")["sign_up"].style is discord.ButtonStyle.primary
    assert "Team_B" not in _buttons("random")
    assert "sign_up" not in _buttons("premade")


@pytest.mark.asyncio
async def test_the_staked_queue_button_explains_the_wallet():
    """Staked is the only type that offers it, and it is the wallet now, not the pool."""
    button = _buttons("staked")["explain_stakes"]
    assert "wallet" in button.label.lower()
    assert "pool" not in button.label.lower()
    assert button.style is discord.ButtonStyle.success
