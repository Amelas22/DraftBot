"""The buttons on the library's DMs: Return on the reminder, Borrow on the
ready notice. A DM has no server, so a press runs the flow for the server the
presser's loan belongs to."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_dm_view_has_fixed_custom_ids():
    from library_views.dm_view import LibraryDmView, dm_view
    persistent = LibraryDmView()
    assert persistent.timeout is None
    assert [i.custom_id for i in persistent.children] == [
        "library_dm:borrow", "library_dm:return"]
    assert [i.custom_id for i in dm_view("borrow").children] == ["library_dm:borrow"]
    assert [i.custom_id for i in dm_view("return").children] == ["library_dm:return"]
    assert [i.label for i in persistent.children] == ["Borrow my deck", "Return my deck"]


async def test_the_dm_view_is_registered_at_startup():
    import re
    src = open("bot.py").read()
    assert re.search(r"bot\.add_view\(LibraryDmView\(\)\)", src)
