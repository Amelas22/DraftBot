"""The cube picker marks the library's cubes, after it is sent.

The badges were written and never shown: nothing called show_library_prices.
Marking reads each library cube from CubeCobra, and a picker has three seconds
to reach Discord, so it is sent first and edited once the badges are known.
A tiered library is quoted as a range, as on the signup board, and a rental
cube another queue has reserved says so rather than counting cards "out".
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cube_views.pack_options as pack
# At import, so the test database creates its table.
import preference_service as prefs
import services.card_library_inventory as inventory
from conftest import a_library
from cube_views.pack_options import mark_library_cubes

pytestmark = pytest.mark.asyncio

GUILD, CUBE, OTHER = "g1", "mycube", "unrelated"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest.fixture(autouse=True)
def cubes(monkeypatch):
    async def fetch(cube_id):
        return list(DECK) if cube_id == CUBE else None
    monkeypatch.setattr(inventory, "fetch_cube", fetch)


def _options():
    return [{"value": CUBE, "label": CUBE}, {"value": OTHER, "label": OTHER}]


async def _rental(**terms):
    await a_library("lounge", guild=GUILD, kind="rental", cubes=(CUBE,),
                    stock={"Swamp": 4}, **terms)


async def test_a_tiered_library_is_quoted_as_a_range(test_db):
    await _rental(collateral=50, collateral_untrusted=100, min_drafts=3)

    marked = await mark_library_cubes(_options(), GUILD)

    mine = next(o for o in marked if o["value"] == CUBE)
    assert mine["description"] == "🏛️ Library cube — 50–100 tix hold", mine
    assert next(o for o in marked if o["value"] == OTHER) == _options()[1], \
        "a cube the library doesn't lend for is left alone"


async def _reserved_queue():
    import services.library_reservation as resv
    from database.db_session import AsyncSessionLocal
    from models.draft_session import DraftSession
    from models.player import PlayerStats

    await _rental(collateral=50, collateral_untrusted=100, min_drafts=3)
    async with AsyncSessionLocal() as s:
        s.add(PlayerStats(player_id="1001", guild_id=GUILD, display_name="Ann",
                          drafts_participated=5))
        now = datetime.now()
        s.add(DraftSession(session_id="q1", guild_id=GUILD, cube=CUBE,
                           sign_ups={"1001": "Ann"}, draft_start_time=now,
                           deletion_time=now + timedelta(hours=3),
                           draft_channel_id="chan", message_id="msg-q1"))
        await s.commit()
    await prefs.set_uses_library("1001", GUILD, True)
    assert (await resv.reserve("q1")).status == "reserved"


async def test_a_rental_cube_another_queue_reserved_says_it_is_in_use(test_db):
    await _reserved_queue()

    marked = await mark_library_cubes(_options(), GUILD)

    mine = next(o for o in marked if o["value"] == CUBE)
    assert mine["emoji"] == "⏳" and "in use by another queue" in mine["description"], mine


async def test_update_cube_does_not_call_its_own_copy_in_use(test_db):
    """The queue that reserved the copy, changing its cube: its current cube is
    held for it, not "in use by another queue" and not "cards out"."""
    await _reserved_queue()

    marked = await mark_library_cubes(_options(), GUILD, own_session_id="q1")

    mine = next(o for o in marked if o["value"] == CUBE)
    assert mine["description"] == "🏛️ Library cube — 50–100 tix hold", mine
    assert mine["emoji"] == "🏛️"


async def test_each_cube_is_read_once_for_the_whole_picker(test_db, monkeypatch):
    calls = []

    async def fetch(cube_id):
        calls.append(cube_id)
        return list(DECK) if cube_id == CUBE else None
    monkeypatch.setattr(inventory, "fetch_cube", fetch)
    await _reserved_queue()
    calls.clear()

    await mark_library_cubes(_options(), GUILD)

    assert calls.count(CUBE) == 1, calls


# --- sent first, marked after -------------------------------------------------

def _picker(monkeypatch):
    from modals import CubeDraftSelectionView
    monkeypatch.setattr(pack, "get_cube_options", lambda guild, kind: _options())
    return CubeDraftSelectionView(session_type="random", guild_id=GUILD)


async def test_the_picker_is_edited_with_its_badges_once_sent(test_db, monkeypatch):
    await _rental(collateral=50, collateral_untrusted=100, min_drafts=3)
    view = _picker(monkeypatch)
    interaction = SimpleNamespace(edit_original_response=AsyncMock())

    await view.mark_once_sent(interaction)

    interaction.edit_original_response.assert_awaited_once_with(view=view)
    labels = {o.value: o.description for o in view.cube_select.options}
    assert labels[CUBE] == "🏛️ Library cube — 50–100 tix hold"


async def test_a_server_with_no_library_is_not_edited(test_db, monkeypatch):
    view = _picker(monkeypatch)
    interaction = SimpleNamespace(edit_original_response=AsyncMock())

    await view.mark_once_sent(interaction)

    interaction.edit_original_response.assert_not_awaited()


async def test_a_failed_edit_leaves_the_picker_working(test_db, monkeypatch):
    await _rental(collateral=50)
    view = _picker(monkeypatch)
    interaction = SimpleNamespace(edit_original_response=AsyncMock(
        side_effect=RuntimeError("interaction expired")))

    await view.mark_once_sent(interaction)   # logged, not raised


async def test_the_draft_command_marks_its_picker_after_sending(monkeypatch):
    import cogs.draft_commands as cmds

    for money, view_name in ((True, "StakedCubeDraftSelectionView"),
                             (False, "CubeDraftSelectionView")):
        view = MagicMock()
        monkeypatch.setattr(cmds, view_name, MagicMock(return_value=view))
        monkeypatch.setattr(cmds, "is_money_server", lambda _gid, m=money: m)
        order = []
        ctx = MagicMock()
        ctx.guild.id = 1
        ctx.response.send_message = AsyncMock(side_effect=lambda *a, **k: order.append("sent"))
        view.mark_once_sent.side_effect = lambda i: order.append("marked")

        await cmds.DraftCommands.draft.callback(cmds.DraftCommands(MagicMock()), ctx)

        view.mark_once_sent.assert_called_once_with(ctx.interaction)
        assert order == ["sent", "marked"], (view_name, order)
