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
            "dmA": {"userName": "RealAlice", "cards": ["c0"], "picks": [
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


def _draft_log_with_dfc():
    return {
        "sessionID": "s1", "time": 1000, "setRestriction": [],
        "users": {
            "dmA": {"userName": "A", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["dfc", "front"], "pick": [0]}]},
        },
        "carddata": {
            # name ALREADY contains the combined "Front // Back" AND has a back face
            "dfc": {"name": "The Legend of Roku // Avatar Roku",
                    "back": {"name": "Avatar Roku"}, "set": "tla"},
            # front-only name with a back face — should be combined exactly once
            "front": {"name": "Delver of Secrets",
                      "back": {"name": "Insectile Aberration"}, "set": "isd"},
        },
    }


def test_dfc_name_not_tripled_when_already_combined():
    h = _helper()
    out = h.convert_to_magicprotools_format(_draft_log_with_dfc(), "dmA")
    # back face must not be appended a second time
    assert "Avatar Roku // Avatar Roku" not in out
    assert "The Legend of Roku // Avatar Roku" in out            # present exactly once, combined
    # a front-only name still gets its back appended once
    assert "Delver of Secrets // Insectile Aberration" in out


def test_pack_first_picks_dfc_name_not_tripled():
    h = _helper()
    picks = h.get_pack_first_picks(_draft_log_with_dfc(), "dmA")
    assert picks["0"] == "The Legend of Roku // Avatar Roku"


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


def _capturing_session(json_body):
    """Like _ok_session but exposes the post mock so tests can inspect the POST body."""
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
    return MagicMock(return_value=outer), sess.post


@pytest.mark.asyncio
async def test_submit_draft_returns_raw_url_and_includes_deck_field():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D&deck=TOK"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        url = await h._submit_draft("dmA", _draft_log(), deck_text="4 Lightning Bolt\n")
    assert url == "https://magicprotools.com/draft/show?id=D&deck=TOK"
    body = post.call_args.kwargs["data"]
    assert body["deck"] == "4 Lightning Bolt\n"
    assert body["apiKey"] == "k" and body["platform"] == "mtgadraft"


@pytest.mark.asyncio
async def test_submit_draft_omits_deck_field_when_no_deck_text():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        url = await h._submit_draft("dmA", _draft_log())
    assert url == "https://magicprotools.com/draft/show?id=D"
    assert "deck" not in post.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_submit_draft_passes_anonymize_flag():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        await h._submit_draft("dmA", _draft_log(), anonymize=True)
    draft_field = post.call_args.kwargs["data"]["draft"]
    assert "RealAlice" not in draft_field and "--> Drafter" in draft_field


@pytest.mark.asyncio
async def test_submit_draft_none_without_key():
    h = MagicProtoolsHelper()
    h.api_key = None
    assert await h._submit_draft("dmA", _draft_log()) is None


@pytest.mark.asyncio
async def test_submit_draft_none_on_error_body():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, _ = _capturing_session({"error": "bad"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        assert await h._submit_draft("dmA", _draft_log(), deck_text="x") is None


@pytest.mark.asyncio
async def test_submit_to_api_attaches_deck_built_from_pool_non_anonymized():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D&deck=TOK"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        url = await h.submit_to_api("dmA", _draft_log())
    assert url == "https://magicprotools.com/draft/show?id=D&deck=TOK"
    body = post.call_args.kwargs["data"]
    assert body["deck"] == "1 Lightning Bolt"       # deck built from dmA's pool
    assert "RealAlice" in body["draft"]              # non-anonymized (real names)


@pytest.mark.asyncio
async def test_submit_to_api_no_deck_field_for_empty_pool():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    log = _draft_log()
    log["users"]["dmA"] = {"userName": "RealAlice", "picks": []}   # no cards → empty deck
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D"})
    with patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        url = await h.submit_to_api("dmA", log)
    assert url == "https://magicprotools.com/draft/show?id=D"
    assert "deck" not in post.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_submit_to_api_degrades_to_draft_only_when_deck_build_raises():
    h = MagicProtoolsHelper()
    h.api_key = "k"
    factory, post = _capturing_session({"url": "https://magicprotools.com/draft/show?id=D"})
    with patch("helpers.magicprotools_helper.build_mtgo_deck_text", side_effect=RuntimeError("boom")), \
         patch("helpers.magicprotools_helper.aiohttp.ClientSession", factory):
        url = await h.submit_to_api("dmA", _draft_log())
    assert url == "https://magicprotools.com/draft/show?id=D"   # still submits
    assert "deck" not in post.call_args.kwargs["data"]          # deck omitted on build failure
