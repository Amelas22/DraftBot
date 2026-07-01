import pytest
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from services.draft_setup_manager import DraftSetupManager


def make_manager(packs_per_player=4, cards_per_pack=10):
    mgr = DraftSetupManager(
        session_id="s",
        draft_id="d",
        cube_id="c",
        guild_id="g",
        packs_per_player=packs_per_player,
        cards_per_pack=cards_per_pack,
    )
    mgr.socket_client = MagicMock()
    mgr.socket_client.connected = True
    mgr.socket_client.emit = AsyncMock(return_value=True)
    return mgr


@pytest.mark.asyncio
async def test_update_draft_settings_emits_pack_settings():
    mgr = make_manager(packs_per_player=4, cards_per_pack=10)

    result = await mgr.update_draft_settings()

    assert result is True
    emitted = {
        call.args[0]: (call.args[1] if len(call.args) > 1 else None)
        for call in cast(AsyncMock, mgr.socket_client.emit).call_args_list
    }
    assert emitted.get("boostersPerPlayer") == 4
    assert emitted.get("cardsPerBooster") == 10


@pytest.mark.asyncio
async def test_update_pack_settings_emits_and_updates_attrs():
    mgr = make_manager(packs_per_player=3, cards_per_pack=15)

    result = await mgr.update_pack_settings(6, 12)

    assert result is True
    assert mgr.packs_per_player == 6
    assert mgr.cards_per_pack == 12
    emitted = {
        call.args[0]: (call.args[1] if len(call.args) > 1 else None)
        for call in cast(AsyncMock, mgr.socket_client.emit).call_args_list
    }
    assert emitted.get("boostersPerPlayer") == 6
    assert emitted.get("cardsPerBooster") == 12
