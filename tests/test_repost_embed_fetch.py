import importlib

import pytest


def _draft_data():
    return {
        "sessionID": "DB1", "time": 1000, "setRestriction": [],
        "users": {
            "u1": {"userName": "Alice", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0"], "pick": [0]}]},
            "u2": {"userName": "Bob", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c0"], "pick": [0]}]},
        },
        "carddata": {"c0": {"name": "Lightning Bolt", "set": "lea"}},
    }


def test_player_logs_from_draft_data_covers_every_user():
    mod = importlib.import_module("scripts.repost_embed")
    logs = mod.player_logs_from_draft_data(_draft_data())
    assert set(logs.keys()) == {"u1", "u2"}
    assert logs["u1"]["name"] == "Alice"
    assert "Lightning Bolt" in logs["u1"]["log_text"]      # MPT text derived from JSON
