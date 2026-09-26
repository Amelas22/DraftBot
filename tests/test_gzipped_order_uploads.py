"""Bodies go up gzipped, and never escape _call's never-raise contract.

The serve runs under Wine, whose HTTP stack mishandles a Content-Length body
that arrives across more than one read: it ACKs every byte, then closes without
answering. A 0ms link puts the whole body in one read and hides it -- which is
why this held up locally and against a mock -- while over the 88ms tailnet link
anything past one congestion window dies. Measured against the live lending
serve on 2026-09-26, the edge is bracketed between 11,874B (answered) and
12,124B (dropped).

Chunked is NOT the way out. The serve does not dechunk at all: curl, aiohttp
bytes, an async generator and 8KB pieces all reach it and all come back with
``{"error":"bad json"}`` off an empty body. That is also why the client must
gzip by hand rather than pass aiohttp's compress="gzip", which would force
chunked framing.

Compression is. Measured on 300 REAL PowerLSV card names -- the shape a full
chunk actually has -- 12,544B raw and 3,584B gzipped, 3.5:1. Synthetic names
sharing a prefix compress 16:1 and flatter the figure badly, so the fixture
below uses real ones.

Both serves were checked the same day: /deposit, /borrow, /return, /withdraw
and /request all decompress on the lending account, and /deposit, /request and
/withdraw on the wallet account.
"""
import gzip
import json as jsonlib
import os
import pathlib
from unittest.mock import MagicMock

import aiohttp
import pytest
from yarl import URL

from services.mtgo_tradebot_client import MAX_BODY_BYTES, MtgoTradeBotClient

# Real PowerLSV names. Their length and variety are the point: a fixture of
# "Card Name Number 0000".. would certify a margin production never sees.
REAL_CARD_NAMES = [
    "Ajani, Nacatl Pariah // Ajani, Nacatl Avenger", "Arena of Glory",
    "Bastion of Remembrance", "Biomechan Engineer", "Blade Splicer", "Brain Freeze",
    "Breeding Pool", "Chandra, Torch of Defiance", "Chaos Defiler", "Cloudkin Seer",
    "Colossal Dreadmask", "Drowner of Truth // Drowned Jungle", "Emrakul, the Aeons Torn",
    "Fastbond", "Fatal Push", "Fell the Profane // Fell Mire", "Figure of Fable",
    "Gemstone Mine", "Ghostfire Slice", "Hedge Maze", "Illuminating Lash",
    "Inti, Seneschal of the Sun", "Kitesail Freebooter", "Monastery Swiftspear",
    "Murderous Redcap", "Orcish Lumberjack", "Outland Liberator // Frenzied Trapbreaker",
    "Palace Jailer", "Paradise Druid", "Patriar's Humiliation",
    "Phlage, Titan of Fire's Fury", "Phyrexian Metamorph", "Portal to Phyrexia",
    "Raugrin Triome", "Reflection Net", "Remand", "Risen Reef", "Sandman's Quicksand",
    "Scion of Draco", "Securitron Squadron", "Shadowy Backstreet", "Soul-Guide Lantern",
    "Spectacle Summit", "Spellseeker", "Splitskin Doll", "Strip Mine", "Subtlety",
    "Suppression Ray // Orderly Plaza", "Sylvan Library", "Taiga",
    "Tamiyo, Collector of Tales", "Temple Garden", "Tersa Lightshatter",
    "Tezzeret the Seeker", "The Ooze", "Torgal, A Fine Hound", "True-Name Nemesis",
    "Vanille, Cheerful l'Cie", "Xander's Wake", "Zephyr Sentinel",
]


def _realistic_order(n=300):
    """n distinct REAL card names, from the tracked draft log plus the list above.

    300 is what a full trade carries. The size matters to the tests below: raw
    this is ~12.3KB, over MAX_BODY_BYTES, while gzipped it is ~3.3KB, under it.
    A fixture that is small either way cannot tell a guard on the compressed
    size from a guard on the raw size -- and the second would reject every
    legitimate full trade.
    """
    log = pathlib.Path(__file__).parent / "fixtures" / "draft_log_6seat.json"
    names = {c["name"] for c in
             (jsonlib.loads(log.read_text()).get("carddata") or {}).values()}
    names |= set(REAL_CARD_NAMES)
    picked = sorted(names)[:n]
    assert len(picked) == n, f"only {len(picked)} distinct real names available"
    return [{"name": x, "qty": 1} for x in picked]


def _client_recording_kwargs():
    """A client whose session records the kwargs each request was given."""
    client = MtgoTradeBotClient(url="http://serve", token="t")
    seen: "list[dict]" = []

    class _Resp:
        status = 202

        async def text(self):
            return '{"id": "job-1"}'

        async def json(self):
            return {"id": "job-1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    def request(_method, _url, **kw):
        seen.append(kw)
        return _Resp()

    session = MagicMock()
    session.request = request
    client._get_session = lambda: session
    return client, seen


def _sent_body(kw):
    """The payload as the serve will read it: gunzipped, then parsed."""
    raw = kw.get("data")
    assert raw is not None, "no body was sent"
    return jsonlib.loads(gzip.decompress(raw).decode())


@pytest.mark.asyncio
async def test_an_order_body_is_gzipped():
    client, seen = _client_recording_kwargs()

    await client.deposit("someone", [{"name": "Swamp", "qty": 1}])

    kw = seen[0]
    assert kw.get("json") is None, "json= would send it uncompressed"
    assert kw["headers"]["Content-Encoding"] == "gzip"
    assert kw["headers"]["Content-Type"] == "application/json"
    assert kw["headers"]["Authorization"] == "Bearer t"
    gzip.decompress(kw["data"])  # raises if it is not actually gzip


@pytest.mark.asyncio
async def test_a_full_trade_of_real_names_is_accepted_on_its_compressed_size():
    """The discriminating case: a real 300-card trade is OVER MAX_BODY_BYTES raw
    and UNDER it gzipped. It must be sent, which is only true if the guard
    measures the compressed bytes -- a guard on the raw size would reject every
    full trade the library ever makes."""
    client, seen = _client_recording_kwargs()

    result = await client.deposit("someone", _realistic_order(300))

    assert seen, "a legitimate full trade was refused"
    sent = seen[0]["data"]
    raw = len(gzip.decompress(sent))
    assert raw > MAX_BODY_BYTES, (
        f"fixture is only {raw}B raw; it cannot tell a compressed-size guard "
        f"from a raw-size one")
    assert len(sent) < MAX_BODY_BYTES, (
        f"{len(sent)}B gzipped is over the {MAX_BODY_BYTES}B guard")
    assert result is not None


@pytest.mark.asyncio
async def test_the_request_is_framed_with_content_length_not_chunked():
    """The serve reads Content-Length and cannot dechunk at all, so framing is
    not an implementation detail -- it is the difference between working and
    "bad json" off an empty body. Asserting the flag we pass is not enough:
    build the request aiohttp would actually put on the wire and read its
    headers."""
    client, seen = _client_recording_kwargs()

    await client.deposit("someone", _realistic_order(300))

    kw = seen[0]
    req = aiohttp.ClientRequest(
        "POST", URL("http://serve/deposit"),
        headers=kw["headers"],
        **{k: v for k, v in kw.items()
           if k in ("data", "chunked", "compress")})
    assert req.headers.get("Transfer-Encoding") is None, (
        "chunked framing -- the serve reads this as an empty body")
    assert req.headers.get("Content-Length") == str(len(kw["data"])), (
        "Content-Length must match the compressed body the serve will read")


@pytest.mark.asyncio
async def test_a_body_over_the_ceiling_is_refused_definitely():
    """Past the serve's ceiling it ACKs and hangs up, which reads as ambiguous
    and sends an order to a human. Refusing first keeps it a clean failure."""
    client, seen = _client_recording_kwargs()
    # incompressible, so it cannot be squeezed back under the limit
    cards = [{"name": os.urandom(16).hex(), "qty": 1} for _ in range(2000)]

    result = await client.deposit("someone", cards)

    assert result is None, "an oversized body must be a definite failure, not ambiguous"
    assert seen == [], "nothing should have been put on the wire"


@pytest.mark.asyncio
async def test_an_unserialisable_payload_returns_rather_than_raises():
    """Callers commit money BEFORE this call and unwind on the return value.
    An exception here skips the unwind: the tix are gone, no job row exists for
    the watchdog to find, and the player is never told."""
    client, seen = _client_recording_kwargs()

    result = await client.deposit("someone", [{"name": object(), "qty": 1}])

    assert result is None, "must be a definite failure, so the caller refunds"
    assert seen == [], "nothing should have been put on the wire"


@pytest.mark.asyncio
async def test_per_card_amounts_survive_the_round_trip():
    """Per-card amounts go as items[] with NO qty -- the serve refuses both."""
    client, seen = _client_recording_kwargs()

    await client.deposit("someone", [{"name": "Swamp", "qty": 2}])

    sent = _sent_body(seen[0])
    assert sent["user"] == "someone"
    assert sent["items"] == [{"name": "Swamp", "qty": 2}]
    assert "qty" not in sent


@pytest.mark.asyncio
async def test_plain_names_still_carry_a_qty():
    """The other shape: names in cards[], with one qty for each of them."""
    client, seen = _client_recording_kwargs()

    await client.deposit("someone", ["Swamp", "Island"], qty=3)

    sent = _sent_body(seen[0])
    assert sent["cards"] == ["Swamp", "Island"]
    assert sent["qty"] == 3


@pytest.mark.asyncio
async def test_a_read_sends_no_body_and_claims_no_encoding():
    """A GET has nothing to compress, and must not advertise otherwise."""
    client, seen = _client_recording_kwargs()

    await client.health()

    kw = seen[0]
    assert kw.get("data") is None
    assert kw.get("json") is None
    assert "Content-Encoding" not in (kw.get("headers") or {})
