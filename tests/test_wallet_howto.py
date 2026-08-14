"""The one place that tells players how to pay with tix.

The copy lives in a single helper because it appears on four surfaces; the thing it
has to get right is not appearing on the servers where /wallet is refused.
"""
from unittest.mock import patch

from helpers.money_gate import WALLET_HOWTO_TITLE, wallet_howto

GUILD = "g1"


def _on_money_server(value=True):
    return patch("helpers.money_gate.is_money_server", return_value=value)


def test_nothing_on_a_server_without_the_wallet():
    """Card loans put debts on free servers too, so the debt panels render there —
    and /wallet is refused there. Telling someone to run it would be worse than
    saying nothing."""
    with _on_money_server(False):
        assert wallet_howto(GUILD) is None
        assert wallet_howto(GUILD, brief=True) is None


def test_names_the_command_that_settles_a_debt():
    with _on_money_server():
        text = wallet_howto(GUILD)

    assert "/wallet deposit" in text
    # the part nobody guesses: funding a wallet pays what you owe by itself
    assert "automatic" in text.lower()


def test_the_brief_form_is_one_line():
    """It sits above a dropdown on the staked sign-up prompt, so a block would bury
    the control it is introducing."""
    with _on_money_server():
        brief = wallet_howto(GUILD, brief=True)

    assert "\n" not in brief
    assert "/wallet deposit" in brief


def test_the_title_is_shared_so_the_panels_match():
    assert "tix" in WALLET_HOWTO_TITLE.lower()


def test_the_copy_only_names_commands_that_exist():
    """The instructions said `/wallet` for a balance, but the bot registers a wallet
    GROUP whose balance subcommand is `show` — so the one line telling a player how to
    look at their own tix named something they cannot run. Pin the copy to the real
    command surface rather than to the cog's docstring, which has the same error.

    Checks every backticked /wallet token, not just those with a word after them: a
    bare `/wallet` is exactly the failure this exists to catch.
    """
    import re

    from cogs.wallet_cog import WalletCommands

    registered = {c.name for c in WalletCommands.wallet.subcommands}
    with _on_money_server():
        copy = f"{wallet_howto(GUILD)} {wallet_howto(GUILD, brief=True)}"

    snippets = re.findall(r"`(/wallet[^`]*)`", copy)
    assert snippets, "the copy should name at least one wallet command"
    for snippet in snippets:
        parts = snippet.split()
        assert len(parts) >= 2, f"{snippet!r} names the group, not a runnable command"
        assert parts[1] in registered, f"{snippet!r} is not a registered command"
