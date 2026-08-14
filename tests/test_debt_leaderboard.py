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


def test_top_involved_field_is_prepended():
    rows = [_row("bob", "alice", -120)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(
            _guild(), rows, top_creditors=[("alice", 3), ("bob", 2)])
    first = pages[0].fields[0]
    assert first.name == "🏆 Most Outstanding"
    assert "🥇 User-alice — 3 debts" in first.value
    assert "🥈 User-bob — 2 debts" in first.value
    # It comes before the outstanding-debts field.
    assert pages[0].fields[1].name.startswith("Outstanding Debts")


def test_singular_debt_label():
    rows = [_row("bob", "alice", -120)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(_guild(), rows, top_creditors=[("alice", 1)])
    assert "🥇 User-alice — 1 debt" in pages[0].fields[0].value


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


def test_field_on_every_page():
    # 12 rows, per_page=10 -> 2 pages; leaderboard on BOTH pages now.
    rows = [_row(f"d{i}", f"c{i}", -(i + 1)) for i in range(12)]
    with _patch_names():
        pages = build_guild_debt_embed_pages(
            _guild(), rows, per_page=10, top_creditors=[("alice", 5)])
    assert len(pages) == 2
    assert pages[0].fields[0].name == "🏆 Most Outstanding"
    assert pages[1].fields[0].name == "🏆 Most Outstanding"


# ---- the wallet how-to block on the debt panels ---------------------------------

def _money_server(value=True):
    """Patch the real gate, not the helper under test: patching add_wallet_howto would
    make the silent-on-free-servers test assert that a stubbed no-op does nothing."""
    return patch("helpers.money_gate.is_money_server", return_value=value)


def test_guild_summary_carries_the_wallet_howto_once_on_the_last_page():
    """Identical on every page, so repeating it through a long list would push the
    debts themselves off screen."""
    rows = [_row(f"p{i}", "alice", -10) for i in range(25)]
    with _patch_names(), _money_server():
        pages = build_guild_debt_embed_pages(_guild(), rows, per_page=10)

    assert len(pages) > 1
    carrying = [i for i, p in enumerate(pages)
                if any("wallet deposit" in f.value for f in p.fields)]
    assert carrying == [len(pages) - 1]


def test_debt_panels_stay_silent_where_there_is_no_wallet():
    """Card loans book debts on free servers too, and /wallet is refused there."""
    from debt_views.helpers import build_user_balance_embed

    rows = [_row("bob", "alice", -10)]
    with _patch_names(), _money_server(False):
        pages = build_guild_debt_embed_pages(_guild(), rows)
        balances = build_user_balance_embed(_guild(), {"alice": -10})

    for embed in (*pages, balances):
        assert not any("wallet" in f.value.lower() for f in embed.fields)


def test_personal_balances_panel_carries_the_wallet_howto():
    from debt_views.helpers import build_user_balance_embed

    with _patch_names(), _money_server():
        embed = build_user_balance_embed(_guild(), {"alice": -10})

    assert any("wallet deposit" in f.value for f in embed.fields)


def test_a_player_who_is_only_owed_money_is_not_told_how_to_pay():
    """The block explains settling a debt. Someone with nothing to settle doesn't
    need it taking up their panel."""
    from debt_views.helpers import build_user_balance_embed

    with _patch_names(), _money_server():
        owes = build_user_balance_embed(_guild(), {"alice": -10})
        owed = build_user_balance_embed(_guild(), {"alice": 10})

    assert any("wallet deposit" in f.value for f in owes.fields)
    assert not any("wallet deposit" in f.value for f in owed.fields)
