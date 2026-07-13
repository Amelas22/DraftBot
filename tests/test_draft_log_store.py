from services.draft_log_store import render_pool


def _log():
    return {
        "carddata": {
            "c1": {"name": "Lightning Bolt"},
            "c2": {"name": "Counterspell"},
            "c3": {"name": "Fable of the Mirror-Breaker"},  # DFC front only
        },
        "users": {
            "u1": {"userName": "Alice", "cards": ["c1", "c1", "c2", "c3"]},
            "u2": {"userName": "Bob", "cards": []},
        },
    }


def test_render_pool_aggregates_counts_and_uses_names():
    out = render_pool(_log(), "u1")
    lines = out.splitlines()
    assert "2 Lightning Bolt" in lines
    assert "1 Counterspell" in lines
    assert "1 Fable of the Mirror-Breaker" in lines
    assert len(lines) == 3


def test_render_pool_empty_for_unknown_or_cardless_user():
    assert render_pool(_log(), "u2") == ""
    assert render_pool(_log(), "nope") == ""
