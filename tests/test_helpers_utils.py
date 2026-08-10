"""helpers/utils.py: the not_none narrowing helper and cube thumbnails.

not_none is shared typing infrastructure (CLAUDE.md's "Type Checking"
conventions tell new code to use it), so its contract is pinned here:
identity passthrough, and an explicit raise — NOT an `assert`, which
`python -O` would strip silently.
"""
import pytest

from helpers.utils import DEFAULT_THUMBNAIL, get_cube_thumbnail_url, not_none


def test_not_none_returns_the_value_unchanged():
    sentinel = object()
    assert not_none(sentinel) is sentinel
    assert not_none(0) == 0            # falsy values are NOT None
    assert not_none("") == ""


def test_not_none_raises_valueerror_on_none():
    with pytest.raises(ValueError):
        not_none(None)


def test_cube_thumbnail_falls_back_to_default():
    assert get_cube_thumbnail_url("NoSuchCube") == DEFAULT_THUMBNAIL
    assert get_cube_thumbnail_url(None) == DEFAULT_THUMBNAIL
    assert get_cube_thumbnail_url("LSVCube") != DEFAULT_THUMBNAIL


def test_as_messageable_narrows_and_rejects():
    import discord
    from unittest.mock import MagicMock

    from helpers.utils import as_messageable

    messageable = MagicMock(spec=discord.TextChannel)
    assert as_messageable(messageable) is messageable

    with pytest.raises(TypeError):
        as_messageable(MagicMock(spec=discord.CategoryChannel))
    with pytest.raises(TypeError):
        as_messageable(None)


def test_ui_button_attribute_is_the_button_item():
    """The whole point of ui_button: after View init, the decorated attribute
    IS the Button item, so attribute access needs no casts."""
    import discord

    from helpers.utils import ui_button

    class _V(discord.ui.View):
        @ui_button(label="Go", custom_id="x")
        async def go_button(self, button, interaction):
            pass

    import asyncio

    async def check():
        v = _V(timeout=1)
        assert isinstance(v.go_button, discord.ui.Button)
        v.go_button.disabled = True          # typed attribute access, no cast
        assert v.go_button.custom_id == "x"

    asyncio.run(check())
