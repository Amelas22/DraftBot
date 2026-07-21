from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from helpers.pack_compositor import PackCompositor


def _img():
    return Image.new("RGB", (244, 340), (0, 0, 0))


def _carddata(n):
    return {f"c{i}": {"name": f"Card {i}", "image_uris": {"normal": f"http://x/{i}.jpg"}} for i in range(n)}


@pytest.mark.asyncio
async def test_composite_none_when_any_card_unfetchable():
    ids = [f"c{i}" for i in range(15)]
    cd = _carddata(15)

    async def fetch(session, card_id, carddata, **kw):
        return None if card_id == "c7" else _img()

    with patch("helpers.pack_compositor.fetch_card_image", new=AsyncMock(side_effect=fetch)):
        out = await PackCompositor().create_pack_composite(ids, cd)
    assert out is None


@pytest.mark.asyncio
async def test_composite_none_when_fetch_raises():
    ids = [f"c{i}" for i in range(15)]
    cd = _carddata(15)
    with patch("helpers.pack_compositor.fetch_card_image",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await PackCompositor().create_pack_composite(ids, cd)
    assert out is None


@pytest.mark.asyncio
async def test_composite_ok_when_all_fetched():
    ids = [f"c{i}" for i in range(15)]
    cd = _carddata(15)
    with patch("helpers.pack_compositor.fetch_card_image", new=AsyncMock(return_value=_img())):
        out = await PackCompositor().create_pack_composite(ids, cd)
    assert isinstance(out, BytesIO)
    assert len(out.getvalue()) > 0
