import urllib.parse

import pytest
from unittest.mock import AsyncMock, MagicMock

from helpers.magicprotools_helper import MagicProtoolsHelper
from helpers.digital_ocean_helper import UploadResult


def _two_user_log():
    return {
        "sessionID": "s1", "time": 1000, "setRestriction": [],
        "users": {
            "dmA": {"userName": "Alice", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0"], "pick": [0]}]},
            "dmB": {"userName": "Bob", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0"], "pick": [0]}]},
        },
        "carddata": {"c0": {"name": "Lightning Bolt", "set": "lea"}},
    }


def _helper_with_uploads_ok():
    h = MagicProtoolsHelper()
    h.api_key = None  # skip direct API submission -> deterministic import-URL fallback
    h.do_helper = MagicMock()
    h.do_helper.upload_text = AsyncMock(return_value=UploadResult(success=True, object_path="k"))
    h.do_helper.get_public_url = lambda key: f"https://cdn/{key}"
    return h


@pytest.mark.asyncio
async def test_upload_draft_logs_accumulates_every_user():
    """The returned map must contain an entry per player. Regression guard for a
    shadowed accumulator that used to drop everyone after the first player and
    return the last UploadResult instead of the dict."""
    h = _helper_with_uploads_ok()

    result = await h.upload_draft_logs(_two_user_log(), "s1", "team")

    assert set(result.keys()) == {"dmA", "dmB"}
    assert result["dmA"]["name"] == "Alice"
    assert result["dmB"]["name"] == "Bob"
    assert result["dmA"]["txt_url"] == "https://cdn/draft_logs/team/s1/DraftLog_dmA.txt"


@pytest.mark.asyncio
async def test_upload_draft_logs_uses_import_url_fallback_without_api_key():
    h = _helper_with_uploads_ok()

    result = await h.upload_draft_logs(_two_user_log(), "s1", "team")

    txt_url = "https://cdn/draft_logs/team/s1/DraftLog_dmB.txt"
    expected = f"https://magicprotools.com/draft/import?url={urllib.parse.quote(txt_url)}"
    assert result["dmB"]["mpt_url"] == expected
