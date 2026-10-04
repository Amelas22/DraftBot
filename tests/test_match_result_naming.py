"""A match is named the same way whichever way you open its result dropdown.

There are two entry points -- the `/report_results` command and the "Match N Results"
button -- and they named the players differently. The command ran them through
`get_display_name`, which prepends ring-bearer/crown icons and applies
`escape_markdown`; the button took `member.display_name` raw.

Neither decoration survives contact with the destination. Discord renders no markdown
in a `SelectOption` label or a `Select` placeholder, so `escape_markdown` put a literal
backslash in front of any `_ * ~ \\`` in a player's name, and the icons arrived in a
dropdown that `display_names.get_member_name_plain` already says should not have them.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import utils
from helpers.display_names import get_display_name, get_member_name_plain


# A name that exercises both decorations: an underscore for escape_markdown, and a
# role that get_display_name would prepend an icon for.
MARKDOWN_NAME = "Jo_hn*Doe"


def _guild(member_by_id):
    guild = MagicMock()
    guild.get_member.side_effect = lambda i: member_by_id.get(int(i))
    guild.id = 1
    return guild


def _member(display_name, roles=()):
    m = MagicMock(spec=["display_name", "roles", "id"])
    m.display_name = display_name
    m.roles = list(roles)
    return m


def test_get_display_name_would_mangle_a_dropdown_label():
    """Why the fix is needed, pinned so the reasoning cannot rot.

    If this ever stops escaping, `get_display_name` became safe for a dropdown and
    the distinction this module exists for is gone.
    """
    member = _member(MARKDOWN_NAME)
    assert get_display_name(member) != MARKDOWN_NAME
    assert "\\_" in get_display_name(member)


def test_plain_naming_leaves_the_name_alone():
    guild = _guild({7: _member(MARKDOWN_NAME)})
    assert get_member_name_plain(guild, "7") == MARKDOWN_NAME


@pytest.mark.asyncio
async def test_fetch_match_details_names_a_dropdown_plainly(monkeypatch):
    """The shared resolver, which both entry points now go through."""
    match = SimpleNamespace(match_number=3, player1_id="7", player2_id="8")
    draft = SimpleNamespace(match_results=[match], guild_id="1")

    monkeypatch.setattr(utils, "AsyncSessionLocal", _fake_session_factory(draft))
    bot = MagicMock()
    bot.get_guild.return_value = _guild({7: _member(MARKDOWN_NAME),
                                         8: _member("Plain Pat")})

    names = await utils.fetch_match_details(bot, "s1", 3)
    assert names == (MARKDOWN_NAME, "Plain Pat")
    assert "\\" not in names[0], "a dropdown label must not carry markdown escapes"


@pytest.mark.asyncio
async def test_a_departed_player_is_identified_rather_than_anonymous(monkeypatch):
    """Both players used to come back as "Unknown Player", which named neither.

    Their opponent still has to file the result, so the report must stay possible
    and the two sides must stay distinguishable.
    """
    match = SimpleNamespace(match_number=3, player1_id="7", player2_id="8")
    draft = SimpleNamespace(match_results=[match], guild_id="1")
    monkeypatch.setattr(utils, "AsyncSessionLocal", _fake_session_factory(draft))
    bot = MagicMock()
    bot.get_guild.return_value = _guild({7: _member("Still Here")})  # 8 has left

    p1, p2 = await utils.fetch_match_details(bot, "s1", 3)
    assert p1 == "Still Here"
    assert p2 == "User 8"


def _fake_session_factory(draft_session):
    """Stand in for AsyncSessionLocal, returning one draft session from execute()."""
    class _Result:
        def scalars(self):
            return SimpleNamespace(first=lambda: draft_session)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    return lambda: _Session()
