"""
Tests for settlement_method: splitting a pair's settled-since-draft tix into
wallet-drawn vs. externally-recorded buckets, and the post-draft stake
summary's wording that reads off that split.

Before this, every settlement row (whether auto-drawn from a player's wallet
by mtgo_resolution_service, or recorded after the fact via the manual
/settle path in debt_service.create_settlement) rendered as "Paid ... from
{loser}'s wallet" -- wrong for the common case, since in production only
~4% of settlements are wallet payments.
"""
import pytest

from conftest import seed_settlement as _settle
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.stake_pairing import StakePairing
from services.debt_service import (
    create_ledger_entries,
    create_settlement,
    get_pair_position_around_draft,
)
from utils import get_formatted_bet_outcomes


class TestGetPairPositionAroundDraft:
    """Service-level split of settled-since-draft tix into wallet vs. external."""

    @pytest.mark.asyncio
    async def test_wallet_settlement_lands_in_settled_wallet(self, test_db):
        # Loser (alice) owes winner (bob) 30 from this draft.
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=30, source_type="draft", source_id="s1",
        )
        # Auto-drawn from alice's wallet, as mtgo_resolution_service writes it.
        await _settle("g1", "alice", "bob", 30, "wallet", "settle-1")

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert pre_existing == 0
        assert settled_wallet == 30
        assert settled_external == 0

    @pytest.mark.asyncio
    async def test_external_settlement_lands_in_settled_external(self, test_db):
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=30, source_type="draft", source_id="s1",
        )
        # The real manual /settle path -- create_settlement now sets
        # settlement_method='external' itself.
        await create_settlement(
            guild_id="g1", payer_id="alice", payee_id="bob",
            amount=30, settled_by="alice",
        )

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert pre_existing == 0
        assert settled_wallet == 0
        assert settled_external == 30

    @pytest.mark.asyncio
    async def test_mix_splits_correctly(self, test_db):
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=50, source_type="draft", source_id="s1",
        )
        await _settle("g1", "alice", "bob", 20, "wallet", "settle-w")
        await create_settlement(
            guild_id="g1", payer_id="alice", payee_id="bob",
            amount=10, settled_by="alice",
        )

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert pre_existing == 0
        assert settled_wallet == 20
        assert settled_external == 10

    @pytest.mark.asyncio
    async def test_null_settlement_method_counts_as_external(self, test_db):
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=30, source_type="draft", source_id="s1",
        )
        # Simulates a settlement row some path forgot to classify -- must
        # never read as a wallet debit the player never authorised.
        await _settle("g1", "alice", "bob", 30, None, "settle-null")

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert settled_wallet == 0
        assert settled_external == 30

    @pytest.mark.asyncio
    async def test_unknown_settlement_method_counts_as_external(self, test_db):
        """The buckets are exhaustive by construction: 'wallet' is the only
        enumerated case, so a future or malformed value must still land in
        settled_external rather than vanishing from both buckets (which would
        silently understate settled_since and therefore new_balance).
        """
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=30, source_type="draft", source_id="s1",
        )
        await _settle("g1", "alice", "bob", 30, "carrier_pigeon", "settle-unknown")

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert settled_wallet == 0
        assert settled_external == 30

    @pytest.mark.asyncio
    async def test_rows_before_draft_stay_out_of_settled_buckets(self, test_db):
        """Regression pin: the id-based cutoff must still hold. A debt fully
        resolved before this draft's own row is pre-existing history, not
        something paid off since THIS draft -- even though it's the same
        kind of row (source_type='settlement') the settled buckets look for.
        """
        # An older draft debt (bob owes alice 15) settled from bob's wallet,
        # entirely before this draft's own stake debt is booked.
        await create_ledger_entries(
            guild_id="g1", debtor_id="bob", creditor_id="alice",
            amount=15, source_type="draft", source_id="s0",
        )
        await _settle("g1", "bob", "alice", 15, "wallet", "settle-old")

        # This draft's own debt: alice owes bob 30. Its id is the cutoff.
        await create_ledger_entries(
            guild_id="g1", debtor_id="alice", creditor_id="bob",
            amount=30, source_type="draft", source_id="s1",
        )

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert pre_existing == 0  # the old debt and its wallet settlement net to zero
        assert settled_wallet == 0
        assert settled_external == 0


class TestSettlementDisplayWording:
    """The post-draft stake summary picks its wording from the wallet/external
    split. Asserting the ABSENCE of "wallet" in the external-only case is what
    actually pins this bug fix -- before this, every settlement said "wallet".
    """

    async def _seed_draft(self, session_id, guild, loser, winner, amount):
        async with AsyncSessionLocal() as session:
            session.add(DraftSession(session_id=session_id, guild_id=guild))
            session.add(StakePairing(
                session_id=session_id, player_a_id=loser, player_b_id=winner,
                amount=amount,
            ))
            await session.commit()
        # Book the stake debt so get_pair_position_around_draft has a cutoff
        # row for this pair, matching what create_debt_entries_from_stakes
        # would have already written by the time this embed renders.
        await create_ledger_entries(
            guild_id=guild, debtor_id=loser, creditor_id=winner,
            amount=amount, source_type="draft", source_id=session_id,
        )

    def _text(self, lines):
        return "\n".join(lines)

    # Each case books a stake debt, settles part or all of it from a wallet
    # amount and/or an external amount, and checks the resulting wording.
    # must_contain / must_not_contain are checked verbatim; must_not_contain_ci
    # is checked against the lowercased text (used for the "wallet" word,
    # which must never appear when nothing was paid from a wallet -- that
    # absence is the assertion that actually pins the bug fix).
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "amount,wallet_amt,external_amt,must_contain,must_not_contain,must_not_contain_ci",
        [
            pytest.param(
                30, 30, 0,
                ["Paid automatically from Alice's wallet", "Nothing left to settle"],
                ["Settled"], [],
                id="fully_paid_wallet_only",
            ),
            pytest.param(
                30, 0, 30,
                ["Settled", "Nothing left to settle"],
                [], ["wallet"],
                id="fully_paid_external_only",
            ),
            pytest.param(
                30, 20, 10,
                ["Paid automatically from Alice's wallet: 20 tix", "Settled: 10 tix",
                 "Nothing left to settle"],
                [], [],
                id="fully_paid_mixed",
            ),
            pytest.param(
                50, 20, 0,
                ["Paid from wallet: 20 tix", "Still owed: 30 tix"],
                ["Settled"], [],
                id="partially_paid_wallet_only",
            ),
            pytest.param(
                50, 0, 20,
                ["Settled: 20 tix", "Still owed: 30 tix"],
                [], ["wallet"],
                id="partially_paid_external_only",
            ),
        ],
    )
    async def test_settlement_wording(self, test_db, amount, wallet_amt, external_amt,
                                       must_contain, must_not_contain, must_not_contain_ci):
        await self._seed_draft("s1", "g1", "alice", "bob", amount)
        if wallet_amt:
            await _settle("g1", "alice", "bob", wallet_amt, "wallet", "settle-w")
        if external_amt:
            await create_settlement(
                guild_id="g1", payer_id="alice", payee_id="bob",
                amount=external_amt, settled_by="alice",
            )

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        for expected in must_contain:
            assert expected in text, f"expected {expected!r} in:\n{text}"
        for unexpected in must_not_contain:
            assert unexpected not in text, f"unexpected {unexpected!r} in:\n{text}"
        for unexpected in must_not_contain_ci:
            assert unexpected not in text.lower(), f"unexpected {unexpected!r} in:\n{text}"

    @pytest.mark.asyncio
    async def test_negative_bucket_renders_no_line_but_still_sums_into_balance(self, test_db):
        """A settlement in the reverse direction landing after this draft's cutoff
        row can make one bucket negative even while their sum (settled_since) is
        positive -- the old single-netted-figure code could never produce this.
        The negative bucket must not render its line (no "-20 tix" nonsense), but
        the balance figures must still reflect the SUM of both buckets, not a
        clamped one -- that's what proves the fix is in the display, not the math.
        """
        await self._seed_draft("s1", "g1", "alice", "bob", 100)
        # Alice overpays 70 from her wallet.
        await _settle("g1", "alice", "bob", 70, "wallet", "settle-wallet")
        # A reverse-direction external correction: bob pays alice back 20, which
        # lands as -20 on alice's external bucket.
        await _settle("g1", "bob", "alice", 20, "external", "settle-correction")

        pre_existing, settled_wallet, settled_external = await get_pair_position_around_draft(
            guild_id="g1", session_id="s1", player_id="alice", counterparty_id="bob",
        )
        assert settled_wallet == 70
        assert settled_external == -20  # the split itself is NOT clamped

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        # Wallet line renders normally.
        assert "Paid from wallet: 70 tix" in text
        # The negative external bucket renders no line at all -- no "Settled"
        # text and definitely no negative number leaking into the display.
        assert "Settled" not in text
        assert "-20" not in text
        # The balance figure reflects the SUMMED buckets (70 + -20 = 50 paid off
        # 100), not a clamped settled_wallet-only figure (which would read
        # "Still owed: 30 tix" if the arithmetic, not just the display, had been
        # changed to ignore the negative bucket).
        assert "Still owed: 50 tix" in text
