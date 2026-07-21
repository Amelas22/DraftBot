from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from helpers.pile_compositor import bucket_cards, PileImageBuilder


def _cd():
    return {
        "land":  {"name": "Wastes",         "cmc": 0, "mana_cost": "",       "image_uris": {"normal": "u"}},
        "zero":  {"name": "Ornithopter",    "cmc": 0, "mana_cost": "{0}",    "image_uris": {"normal": "u"}},
        "ballista": {"name": "Walking Ballista", "cmc": 0, "mana_cost": "{X}{X}", "image_uris": {"normal": "u"}},
        "one":   {"name": "Bolt",           "cmc": 1, "mana_cost": "{R}",    "image_uris": {"normal": "u"}},
        "seven": {"name": "Titan",          "cmc": 7, "mana_cost": "{5}{G}{G}", "image_uris": {"normal": "u"}},
        "nine":  {"name": "Emrakul",        "cmc": 9, "mana_cost": "{9}",    "image_uris": {"normal": "u"}},
        "amv1":  {"name": "Aardvark",       "cmc": 1, "mana_cost": "{W}",    "image_uris": {"normal": "u"}},
    }


def test_bucketing_lands_mv_and_seven_plus():
    cols = dict(bucket_cards(["land", "zero", "ballista", "one", "amv1", "seven", "nine"], _cd()))
    assert cols["Lands"] == ["land"]                 # empty mana_cost + cmc 0
    assert set(cols["0"]) == {"zero", "ballista"}    # {0} artifact and X-spell stay in MV0
    assert cols["1"] == ["amv1", "one"]              # name-sorted: Aardvark before Bolt
    assert set(cols["7+"]) == {"seven", "nine"}      # cmc>=7 collapse


def test_bucketing_sorts_within_column_by_name():
    cols = dict(bucket_cards(["one", "amv1"], _cd()))
    assert cols["1"] == ["amv1", "one"]              # Aardvark before Bolt


def test_bucketing_absent_card_defaults_to_mv0_not_lands():
    # A card_id with no entry in carddata must not be misclassified as a Land
    # (an empty info dict trivially satisfies the land rule).
    cols = dict(bucket_cards(["ghost"], {}))
    assert cols["0"] == ["ghost"]
    assert "Lands" not in cols


def test_bucketing_duplicate_card_ids_not_collapsed():
    cols = dict(bucket_cards(["one", "one", "one"], _cd()))
    assert cols["1"] == ["one", "one", "one"]


@pytest.mark.asyncio
async def test_build_none_when_any_card_unfetchable():
    cd = _cd()
    async def fetch(session, card_id, carddata, **kw):
        return None if card_id == "one" else Image.new("RGB", (244, 340), (1, 2, 3))
    with patch("helpers.pile_compositor.fetch_card_image", new=AsyncMock(side_effect=fetch)):
        out = await PileImageBuilder().build(["land", "one"], [], cd)
    assert out is None


@pytest.mark.asyncio
async def test_build_none_when_fetch_raises():
    cd = _cd()
    with patch("helpers.pile_compositor.fetch_card_image",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await PileImageBuilder().build(["land", "one"], [], cd)
    assert out is None


@pytest.mark.asyncio
async def test_build_returns_jpeg_bytes_on_success():
    cd = _cd()
    with patch("helpers.pile_compositor.fetch_card_image",
               new=AsyncMock(return_value=Image.new("RGB", (244, 340), (1, 2, 3)))):
        out = await PileImageBuilder().build(["land", "one", "seven"], [], cd)
    assert isinstance(out, BytesIO)
    data = out.getvalue()
    assert data[:2] == b"\xff\xd8"                   # JPEG SOI marker


@pytest.mark.asyncio
async def test_build_returns_jpeg_bytes_for_duplicate_card_ids():
    cd = _cd()
    with patch("helpers.pile_compositor.fetch_card_image",
               new=AsyncMock(return_value=Image.new("RGB", (244, 340), (1, 2, 3)))):
        out = await PileImageBuilder().build(["one", "one", "one"], [], cd)
    assert isinstance(out, BytesIO)
    data = out.getvalue()
    assert data[:2] == b"\xff\xd8"                   # JPEG SOI marker


@pytest.mark.asyncio
async def test_build_canvas_dimensions_match_geometry_formula():
    # 1 land + 3 cards at cmc 1 -> two columns: "Lands" (1 card) and "1"
    # (3 cards, the tallest column).
    cd = _cd()
    cd["a"] = {"name": "Aardvark", "cmc": 1, "mana_cost": "{W}", "image_uris": {"normal": "u"}}
    cd["b"] = {"name": "Bolt", "cmc": 1, "mana_cost": "{R}", "image_uris": {"normal": "u"}}
    cd["c"] = {"name": "Charm", "cmc": 1, "mana_cost": "{U}", "image_uris": {"normal": "u"}}
    card_ids = ["land", "a", "b", "c"]

    builder = PileImageBuilder()
    cols = dict(bucket_cards(card_ids, cd))
    assert cols["Lands"] == ["land"]
    assert cols["1"] == ["a", "b", "c"]
    num_cols = 2
    max_cards_in_col = 3

    cw, ch, nb, b = builder.card_width, builder.card_height, builder.name_bar, builder.border
    expected_w = b + num_cols * (cw + b)
    expected_h = b + (ch + nb * (max_cards_in_col - 1)) + b

    with patch("helpers.pile_compositor.fetch_card_image",
               new=AsyncMock(return_value=Image.new("RGB", (cw, ch)))):
        out = await builder.build(card_ids, [], cd)

    assert isinstance(out, BytesIO)
    img = Image.open(out)
    assert img.size == (expected_w, expected_h)


@pytest.mark.asyncio
async def test_build_renders_sideboard_taller_than_main_only():
    cd = _cd()
    solid = Image.new("RGB", (244, 340), (1, 2, 3))
    with patch("helpers.pile_compositor.fetch_card_image", new=AsyncMock(return_value=solid)):
        main_only = await PileImageBuilder().build(["one", "seven"], [], cd)
        with_side = await PileImageBuilder().build(["one", "seven"], ["land", "amv1"], cd)
    h_main = Image.open(BytesIO(main_only.getvalue())).size[1]
    h_side = Image.open(BytesIO(with_side.getvalue())).size[1]
    assert h_side > h_main            # sideboard section adds height


@pytest.mark.asyncio
async def test_build_none_when_a_sideboard_card_unfetchable():
    cd = _cd()
    async def fetch(session, card_id, carddata, **kw):
        return None if card_id == "amv1" else Image.new("RGB", (244, 340), (1, 2, 3))
    with patch("helpers.pile_compositor.fetch_card_image", new=AsyncMock(side_effect=fetch)):
        out = await PileImageBuilder().build(["one"], ["amv1"], cd)
    assert out is None                # all-or-nothing across main ∪ side


@pytest.mark.asyncio
async def test_sideboard_divider_spans_full_width_when_sideboard_wider():
    # Main deck is narrow (1 MV column); sideboard is wider (3 MV columns:
    # Lands, "1", "7+"). The composed canvas width must follow the wider
    # sideboard block, and the SIDEBOARD divider strip must span that full
    # width -- not just the narrower main block's width.
    cd = _cd()
    solid = Image.new("RGB", (244, 340), (1, 2, 3))
    builder = PileImageBuilder()
    cw, ch, nb, b = builder.card_width, builder.card_height, builder.name_bar, builder.border

    main_ids = ["one"]                    # 1 column ("1"), 1 card tall
    side_ids = ["land", "one", "seven"]   # 3 columns: Lands, "1", "7+" -- each 1 card tall

    main_cols = dict(bucket_cards(main_ids, cd))
    side_cols = dict(bucket_cards(side_ids, cd))
    assert len(main_cols) == 1
    assert len(side_cols) == 3

    main_block_h = b + (ch + nb * (1 - 1)) + b   # tallest main column has 1 card

    with patch("helpers.pile_compositor.fetch_card_image", new=AsyncMock(return_value=solid)):
        out = await builder.build(main_ids, side_ids, cd)

    assert isinstance(out, BytesIO)
    img = Image.open(BytesIO(out.getvalue())).convert("RGB")
    canvas_w, _canvas_h = img.size

    divider_y = main_block_h + 14   # middle of the 28px divider strip
    sample_x = canvas_w - 5         # near the right edge of the canvas
    r, g, bch = img.getpixel((sample_x, divider_y))

    for channel in (r, g, bch):
        assert abs(channel - 40) <= 25, (r, g, bch)
