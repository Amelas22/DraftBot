import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import notification_service
from services.library_prompt import WHERE_TO_CHANGE

RETIRED = ("toggle_dm_notifications", "donation_day anonymous")


def test_no_player_facing_file_names_a_retired_command():
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             check=True).stdout.split()
    hits = [(p, r) for p in tracked
            if not p.startswith(("tests/", "docs/", "alembic/"))
            and p.endswith((".py", ".md", ".json", ".txt"))
            for r in RETIRED if r in Path(p).read_text(errors="ignore")]
    assert hits == []


def test_the_library_points_at_settings_too():
    assert "/settings" in WHERE_TO_CHANGE and "Change Entry / Settings" in WHERE_TO_CHANGE


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["send_ready_check_dms", "send_teams_created_dms"])
async def test_dm_footers_point_at_settings(monkeypatch, sender):
    captured = {}

    async def fake_send(*args):
        captured["builder"] = args[-1]
        return (0, 0)

    monkeypatch.setattr(notification_service, "_send_notification_dms", fake_send)
    session = MagicMock()
    session.get_draft_link_for_user.return_value = "https://draft.example/x"
    await getattr(notification_service, sender)(
        MagicMock(), session, "1", "2", "chan", "Test Guild")
    message = captured["builder"]("Someone", "https://discord.com/channels/1/2")
    assert "_To turn these off, use `/settings` in Test Guild_" in message
