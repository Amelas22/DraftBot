import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from helpers.magicprotools_helper import MagicProtoolsHelper


def _helper():
    return MagicProtoolsHelper()


def test_extract_deck_token():
    h = _helper()
    url = "https://magicprotools.com/draft/show?id=ABC&deck=TOKEN-123_xy"
    assert h.extract_deck_token(url) == "TOKEN-123_xy"
    assert h.extract_deck_token("https://magicprotools.com/draft/show?id=ABC") is None
    assert h.extract_deck_token("") is None
    assert h.extract_deck_token(None) is None


def _draft_log():
    return {
        "sessionID": "s1", "time": 1000,
        "setRestriction": [],
        "users": {
            "dmA": {"userName": "RealAlice", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0", "c1"], "pick": [0]}]},
            "dmB": {"userName": "RealBob", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0", "c1"], "pick": [1]}]},
        },
        "carddata": {"c0": {"name": "Lightning Bolt", "set": "lea"},
                     "c1": {"name": "Llanowar Elves", "set": "lea"}},
    }


def test_anonymize_hides_all_usernames_marks_target():
    h = _helper()
    out = h.convert_to_magicprotools_format(_draft_log(), "dmA", anonymize=True)
    assert "RealAlice" not in out and "RealBob" not in out   # no real names
    assert "--> Drafter" in out                               # target marked + labeled
    assert "Player 1" in out                                  # other player relabeled
    assert "Lightning Bolt" in out                            # real cards kept


def test_non_anonymized_output_unchanged():
    h = _helper()
    out = h.convert_to_magicprotools_format(_draft_log(), "dmA")   # default False
    assert "--> RealAlice" in out and "    RealBob" in out


def _ok_session(json_body):
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=json_body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    sess = MagicMock()
    sess.post = MagicMock(return_value=cm)
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=sess)
    outer.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=outer)


@pytest.mark.asyncio
async def test_submit_deck_view_returns_deck_show_url():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    body = {"url": "https://magicprotools.com/draft/show?id=D&deck=TOK99"}
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", _ok_session(body)):
        url = await h.submit_deck_view("dmA", _draft_log(), "4 Lightning Bolt\n")
    assert url == "https://magicprotools.com/deck/show?id=TOK99"


@pytest.mark.asyncio
async def test_submit_deck_view_none_on_error_body():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession",
               _ok_session({"error": "bad"})):
        assert await h.submit_deck_view("dmA", _draft_log(), "x") is None


@pytest.mark.asyncio
async def test_submit_deck_view_none_without_key():
    h = MagicProtoolsHelper()
    h.api_key = None
    assert await h.submit_deck_view("dmA", _draft_log(), "x") is None
