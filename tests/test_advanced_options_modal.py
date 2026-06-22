import pytest
from unittest.mock import AsyncMock, MagicMock

from modals import AdvancedOptionsModal, parse_pack_settings
from cube_views.pack_options import pack_format_display


def test_pack_format_display_none_for_default():
    assert pack_format_display(3, 15) is None


def test_pack_format_display_string_for_non_default():
    text = pack_format_display(4, 10)
    assert text is not None
    assert "4" in text and "10" in text


class FakeView:
    def __init__(self):
        self.packs_per_player = 3
        self.cards_per_pack = 15


def make_modal(view, packs="3", cards="15"):
    modal = AdvancedOptionsModal(view)
    modal.children[0].value = packs
    modal.children[1].value = cards
    return modal


def make_interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


# ---- parse_pack_settings (pure) ---------------------------------------------

def test_parse_valid_values():
    packs, cards, errors = parse_pack_settings("4", "10")
    assert (packs, cards) == (4, 10)
    assert errors == []


def test_parse_non_numeric_reports_error():
    packs, cards, errors = parse_pack_settings("abc", "10")
    assert packs is None
    assert any("Packs" in e for e in errors)


def test_parse_out_of_range_reports_error():
    _, _, errors = parse_pack_settings("999", "0")
    assert len(errors) == 2  # both out of range


# ---- modal callback ---------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_input_updates_view_and_confirms():
    view = FakeView()
    modal = make_modal(view, packs="4", cards="10")
    interaction = make_interaction()

    await modal.callback(interaction)

    assert view.packs_per_player == 4
    assert view.cards_per_pack == 10
    msg = interaction.response.send_message.call_args.args[0]
    assert "✅" in msg
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_invalid_input_sends_error_and_leaves_view_unchanged():
    view = FakeView()
    modal = make_modal(view, packs="notanumber", cards="10")
    interaction = make_interaction()

    await modal.callback(interaction)

    assert view.packs_per_player == 3  # unchanged
    assert view.cards_per_pack == 15   # unchanged
    msg = interaction.response.send_message.call_args.args[0]
    assert "❌" in msg
