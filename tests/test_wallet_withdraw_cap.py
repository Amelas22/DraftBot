"""The /wallet withdraw amount is capped at the serve's per-trade limit.

Above 300 the serve splits the request into several jobs and returns no single
job id; start_withdraw reads that missing id as a rejection, hands the tix back
to the player, and the serve then delivers them anyway -- tix leave the vault
while the claim is restored. Two such withdrawals (LSV 500, MackSmith 400) put
the vault 800 tix behind the claim ledger.

Capping the option is the containment, not the cure: the fix is for
start_withdraw to understand a split response. Until then the command must not
be able to ask for a split in the first place.
"""
from cogs.wallet_cog import SERVE_TRADE_LIMIT, WalletCommands

def _amount_option():
    withdraw = next(c for c in WalletCommands.wallet.subcommands if c.name == "withdraw")
    return next(o for o in withdraw.options if o.name == "amount")


def test_withdraw_amount_is_capped_at_300():
    """Asserted as a literal, not against SERVE_TRADE_LIMIT: comparing the option
    to the same constant it is built from passes no matter what that constant
    says, so raising it to 500 would reintroduce the bug with the suite green.
    300 is the serve's actual per-trade limit -- if the serve's limit changes,
    this test should have to be changed deliberately."""
    assert _amount_option().max_value == 300


def test_the_cap_constant_is_what_the_option_uses():
    """The two must not drift apart -- the constant is what the help text and
    any future caller read."""
    assert SERVE_TRADE_LIMIT == _amount_option().max_value


def test_withdraw_amount_still_requires_a_positive_number():
    """The cap must not disturb the existing floor."""
    assert _amount_option().min_value == 1
