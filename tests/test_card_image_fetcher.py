import time
from io import BytesIO
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from PIL import Image

from helpers.card_image_fetcher import build_image_url_ladder, fetch_card_image


def _png_bytes():
    buf = BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._body


class _FakeSession:
    """Maps each URL to a queue of _FakeResp; pops per call so a URL can fail
    then succeed. Records the URLs requested in order."""

    def __init__(self, responses: dict):
        self._responses = {u: list(q) for u, q in responses.items()}
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        queue = self._responses.get(url)
        if not queue:
            return _FakeResp(404)
        return queue.pop(0)


CARDDATA = {
    "cid1": {"name": "Lightning Bolt", "image_uris": {"normal": "http://cdn/bolt.jpg"}},
}
CAPTURED = "http://cdn/bolt.jpg"
BY_NAME = "https://api.scryfall.com/cards/named?exact=Lightning%20Bolt&format=image&version=normal"
BY_ID = "https://api.scryfall.com/cards/cid1?format=image&version=normal"

DFC_CARDDATA = {
    "cid2": {
        "name": "Some DFC",
        "card_faces": [
            {"image_uris": {"normal": "http://cdn/face.jpg"}},
            {"image_uris": {"normal": "http://cdn/back.jpg"}},
        ],
    },
}
FACE_URL = "http://cdn/face.jpg"


def test_ladder_order_and_dedup():
    ladder = build_image_url_ladder("cid1", CARDDATA)
    assert ladder[0] == CAPTURED
    assert ladder[-1] == BY_NAME
    assert "cards/cid1?format=image" in ladder[1]
    assert len(ladder) == len(set(ladder))


@pytest.mark.asyncio
async def test_success_on_captured_url_first_try():
    session = _FakeSession({CAPTURED: [_FakeResp(200, _png_bytes())]})
    img = await fetch_card_image(session, "cid1", CARDDATA)
    assert img is not None
    assert session.requested == [CAPTURED]


@pytest.mark.asyncio
async def test_transient_then_success_retries_with_backoff():
    session = _FakeSession({CAPTURED: [_FakeResp(503), _FakeResp(200, _png_bytes())]})
    with patch("helpers.card_image_fetcher.asyncio.sleep", new=AsyncMock()) as slept:
        img = await fetch_card_image(session, "cid1", CARDDATA, base_delay=0.5)
    assert img is not None
    slept.assert_awaited_once_with(0.5)          # one backoff before the retry
    assert session.requested == [CAPTURED, CAPTURED]


@pytest.mark.asyncio
async def test_404_advances_to_name_rung():
    session = _FakeSession({
        CAPTURED: [_FakeResp(404)],
        BY_NAME: [_FakeResp(200, _png_bytes())],
    })
    img = await fetch_card_image(session, "cid1", CARDDATA)
    assert img is not None
    assert session.requested[0] == CAPTURED
    assert session.requested[-1] == BY_NAME       # reached the different-version rung


@pytest.mark.asyncio
async def test_all_rungs_fail_returns_none():
    session = _FakeSession({})                    # every URL -> 404
    with patch("helpers.card_image_fetcher.asyncio.sleep", new=AsyncMock()):
        img = await fetch_card_image(session, "cid1", CARDDATA)
    assert img is None


@pytest.mark.asyncio
async def test_deadline_already_passed_fails_fast():
    session = _FakeSession({CAPTURED: [_FakeResp(200, _png_bytes())]})
    past = time.monotonic() - 1.0
    img = await fetch_card_image(session, "cid1", CARDDATA, deadline=past)
    assert img is None
    assert session.requested == []                # never even attempted


@pytest.mark.asyncio
async def test_dfc_face_rung_used_when_no_top_level_image():
    ladder = build_image_url_ladder("cid2", DFC_CARDDATA)
    assert ladder[0] == FACE_URL                   # no top-level image_uris -> face rung is first

    session = _FakeSession({FACE_URL: [_FakeResp(200, _png_bytes())]})
    img = await fetch_card_image(session, "cid2", DFC_CARDDATA)
    assert img is not None
    assert session.requested == [FACE_URL]


@pytest.mark.asyncio
async def test_retries_exhaust_then_advances_to_next_rung():
    max_retries = 3
    session = _FakeSession({
        CAPTURED: [_FakeResp(503), _FakeResp(503), _FakeResp(503)],
        BY_ID: [_FakeResp(200, _png_bytes())],
    })
    with patch("helpers.card_image_fetcher.asyncio.sleep", new=AsyncMock()) as slept:
        img = await fetch_card_image(
            session, "cid1", CARDDATA, max_retries=max_retries, base_delay=0.5
        )
    assert img is not None
    assert session.requested.count(CAPTURED) == max_retries   # all attempts on first rung used
    assert session.requested[-1] == BY_ID                      # then advanced to next rung
    assert slept.await_count == max_retries - 1                # no wasted sleep on last attempt


@pytest.mark.asyncio
async def test_200_with_non_image_body_advances_to_next_rung():
    # A 200 whose body isn't a decodable image (corrupt/HTML) must not crash
    # with PIL.UnidentifiedImageError - it should be treated as a failed
    # rung and fall through to the next one, without retrying this rung.
    session = _FakeSession({
        CAPTURED: [_FakeResp(200, b"<html>nope")],
        BY_ID: [_FakeResp(200, _png_bytes())],
    })
    img = await fetch_card_image(session, "cid1", CARDDATA)
    assert img is not None
    assert session.requested.count(CAPTURED) == 1               # no retry on same rung
    assert session.requested[-1] == BY_ID                       # advanced to next rung
