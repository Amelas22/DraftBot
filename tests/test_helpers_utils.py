"""helpers/utils.py: the not_none narrowing helper and cube thumbnails.

not_none is shared typing infrastructure (CLAUDE.md's "Type Checking"
conventions tell new code to use it), so its contract is pinned here:
identity passthrough, and an explicit raise — NOT an `assert`, which
`python -O` would strip silently.
"""
import pytest

import helpers.utils as utils
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


# --- send_then_mention -------------------------------------------------------

class _EditableMessage:
    def __init__(self, content):
        self.id = 7
        self.content = content
        self.edit_kwargs = None
        self.edit_error = None

    async def edit(self, **kwargs):
        if self.edit_error:
            raise self.edit_error
        self.edit_kwargs = kwargs
        self.content = kwargs.get("content")


class _Destination:
    def __init__(self, edit_error=None):
        self.sent = None
        self.message = None
        self._edit_error = edit_error

    async def send(self, content):
        self.sent = content
        self.message = _EditableMessage(content)
        self.message.edit_error = self._edit_error
        return self.message


@pytest.mark.asyncio
async def test_send_then_mention_posts_plain_and_edits_the_mention_in():
    """The whole point: a mention that arrives WITH a message notifies everyone
    named, the same mention edited in does not."""
    dest = _Destination()

    await utils.send_then_mention(dest, "starter", "<@a1> starter")

    assert "<@" not in dest.sent, "the created message would have notified"
    assert dest.message.content == "<@a1> starter"


@pytest.mark.asyncio
async def test_send_then_mention_parses_the_mention_on_the_edit():
    """An unparsed mention adds nobody to the thread, which is the only reason
    this helper exists. py-cord falls back to Client.allowed_mentions when the
    argument is omitted, so it is passed explicitly."""
    dest = _Destination()

    await utils.send_then_mention(dest, "starter", "<@a1> starter")

    allowed = dest.message.edit_kwargs["allowed_mentions"]
    assert allowed.users is True and allowed.everyone is False


@pytest.mark.asyncio
async def test_send_then_mention_keeps_the_message_when_the_edit_fails():
    """Best-effort by design: a failed edit costs the sidebar entry, not the
    starter. Every caller treats these as optional."""
    dest = _Destination(edit_error=RuntimeError("nope"))

    message = await utils.send_then_mention(dest, "starter", "<@a1> starter")

    assert message.content == "starter"


@pytest.mark.asyncio
async def test_send_then_mention_skips_the_edit_when_there_is_nobody_to_mention():
    """An empty roster must not cost a second API call for a no-op edit."""
    dest = _Destination()

    await utils.send_then_mention(dest, "starter", "starter")

    assert dest.message.edit_kwargs is None
