from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_setup_manager import DraftSetupManager
from helpers.magicprotools_helper import MagicProtoolsHelper
from helpers.digital_ocean_helper import UploadResult


def test_archive_write_methods_removed():
    """Direct guard for the deprecation: the two methods that wrote the
    per-player .txt archive must no longer exist. (Fails on main.)"""
    assert not hasattr(DraftSetupManager, "process_draft_logs_for_magicprotools")
    assert not hasattr(MagicProtoolsHelper, "upload_draft_logs")


def _draft_data():
    return {
        "sessionID": "DB123", "time": 1000,
        "users": {"u1": {"userName": "Alice", "picks": []},
                  "u2": {"userName": "Bob", "picks": []}},
        "carddata": {},
    }


@pytest.mark.asyncio
async def test_save_to_spaces_uploads_json_and_writes_no_txt():
    mgr = DraftSetupManager.__new__(DraftSetupManager)   # skip __init__
    mgr.session_type = "team"
    mgr.cube_id = "LSVCube"
    mgr.logger = MagicMock()

    do = MagicMock()
    do.upload_json = AsyncMock(return_value=UploadResult(success=True, object_path="team/LSVCube-1000-DB123.json"))
    do.upload_text = AsyncMock(return_value=UploadResult(success=True, object_path="x"))

    with patch("services.draft_setup_manager.DigitalOceanHelper", return_value=do):
        key = await mgr.save_to_digitalocean_spaces(_draft_data())

    assert key == "team/LSVCube-1000-DB123.json"
    do.upload_json.assert_awaited_once()
    do.upload_text.assert_not_awaited()          # no .txt archive written
