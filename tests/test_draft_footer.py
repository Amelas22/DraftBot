"""Unit tests for the universal draft metadata footer (helpers/draft_footer.py)."""
from types import SimpleNamespace

import discord
import pytest

from helpers.draft_footer import (
    apply_draft_footer,
    apply_draft_footer_from_session,
    draft_footer_text,
)
from ready_check import ReadyCheckSession
from sessions.random_session import RandomSession


def _session_details(**overrides):
    details = dict(
        session_id="123456789012345678-1753500000",
        draft_id="A7K2M9QZ",
        friendly_id="lightning-bolt-7",
        cube_choice="LSVCube",
        draft_start_time=1753500000,
        packs_per_player=3,
        cards_per_pack=15,
        guild_id="1",
        team_a_name=None,
        team_b_name=None,
    )
    details.update(overrides)
    return SimpleNamespace(**details)


def test_footer_text_is_labelled_and_free_of_markdown():
    # Footers render markdown literally, so none of it may leak through.
    text = draft_footer_text("A7K2M9QZ", "LSVCube")
    assert text == "ID: A7K2M9QZ • Cube: LSVCube"
    assert "`" not in text
    assert "-#" not in text


def test_missing_pieces_drop_their_labels_too():
    assert draft_footer_text("A7K2M9QZ", None) == "ID: A7K2M9QZ"
    assert draft_footer_text(None, "LSVCube") == "Cube: LSVCube"
    assert draft_footer_text(None, None) == ""


def test_no_footer_is_set_when_there_is_nothing_to_show():
    embed = discord.Embed(title="Draft")
    apply_draft_footer(embed, None, None)
    assert "footer" not in embed.to_dict()


def test_embed_timestamp_is_left_alone():
    # Setting it would render an unlabelled time next to the message's own
    # timestamp, reading as the post time rather than anything about the draft.
    embed = discord.Embed(title="Draft")
    apply_draft_footer(embed, "A7K2M9QZ", "LSVCube")
    assert embed.timestamp is None


def test_from_session_uses_friendly_id_not_draft_id_or_session_id():
    draft_session = SimpleNamespace(
        session_id="123456789012345678-1753500000",
        draft_id="A7K2M9QZ",
        friendly_id="lightning-bolt-7",
        cube="LSVCube",
    )
    embed = apply_draft_footer_from_session(discord.Embed(title="Draft"), draft_session)
    assert embed.footer.text == "ID: lightning-bolt-7 • Cube: LSVCube"
    assert "123456789012345678" not in embed.footer.text
    assert "A7K2M9QZ" not in embed.footer.text


def test_signup_and_later_posts_share_an_identical_footer():
    # The whole point: every post for one draft carries the same stamp.
    signup = RandomSession(
        _session_details(), session_factory=lambda: None
    ).create_embed()
    log = apply_draft_footer_from_session(
        discord.Embed(title="Draft Log"),
        SimpleNamespace(friendly_id="lightning-bolt-7", cube="LSVCube"),
    )
    assert signup.footer.text == log.footer.text == "ID: lightning-bolt-7 • Cube: LSVCube"


@pytest.mark.asyncio
async def test_ready_check_embed_carries_the_footer():
    rc = ReadyCheckSession(["1", "2"])
    embed = await rc.build_embed(
        {"1": "alice", "2": "bob"},
        draft_session=SimpleNamespace(friendly_id="lightning-bolt-7", cube="LSVCube"),
    )
    assert embed.footer.text == "ID: lightning-bolt-7 • Cube: LSVCube"


@pytest.mark.asyncio
async def test_ready_check_embed_omits_footer_without_a_session():
    # draft_session is optional; callers that lack one must not crash.
    rc = ReadyCheckSession(["1"])
    embed = await rc.build_embed({"1": "alice"})
    assert "footer" not in embed.to_dict()


def test_draft_session_embed_carries_the_footer():
    payload = RandomSession(
        _session_details(), session_factory=lambda: None
    ).create_embed().to_dict()

    assert payload["footer"]["text"] == "ID: lightning-bolt-7 • Cube: LSVCube"
    assert "timestamp" not in payload
    # No icon: the cube art is already the embed thumbnail.
    assert "icon_url" not in payload["footer"]


@pytest.mark.asyncio
async def test_update_draft_message_restamps_footer_after_cube_change():
    """#383: Update Cube edited the Cube: field and thumbnail but left the
    footer carrying the old cube name. update_draft_message must re-stamp it."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import views

    stale = discord.Embed(title="Looking for Players!")
    stale.add_field(name="Cube:", value="[LSVCube](https://cubecobra.com/cube/list/LSVCube)", inline=True)
    stale.add_field(name="Sign-Ups:", value="**Players (0):**\nNo players yet.", inline=False)
    stale.set_footer(text="ID: lightning-bolt-7 • Cube: LSVCube")

    session = SimpleNamespace(
        session_id="123456789012345678-1753500000",
        friendly_id="lightning-bolt-7",
        cube="ArenaChrome",  # the session was updated to a new cube
        draft_channel_id="111",
        message_id="222",
        sign_ups={},
        session_type="random",
        packs_per_player=3,
        cards_per_pack=15,
    )

    message = MagicMock()
    message.embeds = [stale]
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.guild = MagicMock()
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch.object(views, "get_draft_session", AsyncMock(return_value=session)):
        await views.update_draft_message(bot, session.session_id)

    message.edit.assert_awaited_once()
    edited = message.edit.await_args.kwargs["embed"]
    assert edited.footer.text == "ID: lightning-bolt-7 • Cube: ArenaChrome"
    cube_field = next(f for f in edited.fields if f.name == "Cube:")
    assert "ArenaChrome" in cube_field.value
