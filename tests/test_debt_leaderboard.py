from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from debt_views.helpers import build_guild_debt_embed_pages


def _row(player_id, counterparty_id, balance):
    return SimpleNamespace(player_id=player_id, counterparty_id=counterparty_id, balance=balance)


def _guild():
    return MagicMock()


# get_member_name(guild, id) -> display name; patch it to a predictable name.
def _patch_names():
    return patch("debt_views.helpers.get_member_name", lambda guild, uid: f"User-{uid}")


def test_top_creditors_field_is_prepended_on_first_page():
    rows = [_row("bob", "alice", -120)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(
            _guild(), rows, top_creditors=[("alice", 150), ("bob", 80)])
    first = pages[0].fields[0]
    assert first.name == "🏆 Most Outstanding"
    assert "🥇 User-alice — 150 tix" in first.value
    assert "🥈 User-bob — 80 tix" in first.value
    # It comes before the outstanding-debts field.
    assert pages[0].fields[1].name.startswith("Outstanding Debts")


def test_no_field_when_top_creditors_none():
    rows = [_row("bob", "alice", -120)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(_guild(), rows)  # default None
    assert all(f.name != "🏆 Most Outstanding" for f in pages[0].fields)


def test_no_field_when_top_creditors_empty():
    rows = [_row("bob", "alice", -120)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(_guild(), rows, top_creditors=[])
    assert all(f.name != "🏆 Most Outstanding" for f in pages[0].fields)


def test_field_only_on_first_page():
    # 12 rows, per_page=10 -> 2 pages; leaderboard only on page 0.
    rows = [_row(f"d{i}", f"c{i}", -(i + 1)) for i in range(12)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(
            _guild(), rows, per_page=10, top_creditors=[("alice", 150)])
    assert pages[0].fields[0].name == "🏆 Most Outstanding"
    assert all(f.name != "🏆 Most Outstanding" for f in pages[1].fields)
