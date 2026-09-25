"""Reading a cube's list off CubeCobra.

Two contracts the deposit command branches on live here, and neither was
covered: how repeats are counted, and the difference between a cube that is
empty and a CubeCobra that did not answer.
"""
from unittest.mock import patch

import pytest

from helpers.cube_list import fetch_cube, parse_cube_list


def test_repeats_become_one_item_with_a_quantity():
    """The serve's items[] takes a quantity, and a cube running four Lightning
    Bolt would otherwise send four line items -- which is also what the deposit
    command counts as "distinct" when it describes the cube."""
    cards = parse_cube_list("Lightning Bolt\nLightning Bolt\nIsland\nLightning Bolt\n")

    assert cards == [{"name": "Lightning Bolt", "qty": 3}, {"name": "Island", "qty": 1}]


def test_blank_lines_and_stray_whitespace_are_not_cards():
    cards = parse_cube_list("  Island  \n\n\n\tSwamp\n   \n")

    assert cards == [{"name": "Island", "qty": 1}, {"name": "Swamp", "qty": 1}]


def test_the_order_cards_appear_in_is_kept():
    """Chunking walks this list in order, so a deposit's trades follow the
    cube's own order and a depositor can see where a run stopped."""
    cards = parse_cube_list("Zoo\nAnt\nMox\n")

    assert [c["name"] for c in cards] == ["Zoo", "Ant", "Mox"]


class _Resp:
    def __init__(self, status, text=""):
        self.status, self._text = status, text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _serving(resp):
    return patch("helpers.cube_list.aiohttp.ClientSession",
                 lambda *a, **kw: _Session(resp))


@pytest.mark.asyncio
async def test_a_cube_that_cannot_be_read_is_none_not_empty():
    """"CubeCobra did not answer" and "the cube is empty" lead to different
    messages -- one is a thing to retry, the other is a cube to fix. Collapsing
    them to [] would tell someone with a typo'd id that their cube is empty."""
    with _serving(_Resp(404)):
        assert await fetch_cube("nope") is None


@pytest.mark.asyncio
async def test_a_network_failure_is_also_none():
    with _serving(OSError("no route to host")):
        assert await fetch_cube("anything") is None


@pytest.mark.asyncio
async def test_an_empty_cube_is_an_empty_list():
    with _serving(_Resp(200, "\n\n")):
        assert await fetch_cube("bare") == []
