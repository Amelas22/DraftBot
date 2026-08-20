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

from database.db_session import AsyncSessionLocal
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.stake_pairing import StakePairing
from services.debt_service import (
    create_ledger_entries,
    create_settlement,
    get_pair_position_around_draft,
)
from utils import get_formatted_bet_outcomes


async def _settle(guild, payer, payee, amount, method, source_id):
    """Insert a pair of settlement rows directly, with an explicit
    settlement_method -- including None, to simulate a row some other path
    forgot to classify.

    Mirrors the amount convention of both real writers: the payer's entry is
    positive (it reduces what they owe), the payee's is negative (it reduces
    what they're owed).
    """
    async with AsyncSessionLocal() as session:
        session.add(DebtLedger(
            guild_id=guild, player_id=payer, counterparty_id=payee,
            amount=amount, source_type='settlement', source_id=source_id,
            settlement_method=method,
        ))
        session.add(DebtLedger(
            guild_id=guild, player_id=payee, counterparty_id=payer,
            amount=-amount, source_type='settlement', source_id=source_id,
            settlement_method=method,
        ))
        await session.commit()


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

    @pytest.mark.asyncio
    async def test_fully_paid_wallet_only(self, test_db):
        await self._seed_draft("s1", "g1", "alice", "bob", 30)
        await _settle("g1", "alice", "bob", 30, "wallet", "settle-1")

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        assert "Paid automatically from Alice's wallet" in text
        assert "Nothing left to settle" in text
        assert "Settled" not in text

    @pytest.mark.asyncio
    async def test_fully_paid_external_only(self, test_db):
        await self._seed_draft("s1", "g1", "alice", "bob", 30)
        await create_settlement(
            guild_id="g1", payer_id="alice", payee_id="bob",
            amount=30, settled_by="alice",
        )

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        assert "Settled" in text
        assert "Nothing left to settle" in text
        # The assertion that actually pins the bug: no wallet wording at all
        # for a settlement that never touched the wallet.
        assert "wallet" not in text.lower()

    @pytest.mark.asyncio
    async def test_fully_paid_mixed(self, test_db):
        await self._seed_draft("s1", "g1", "alice", "bob", 30)
        await _settle("g1", "alice", "bob", 20, "wallet", "settle-w")
        await create_settlement(
            guild_id="g1", payer_id="alice", payee_id="bob",
            amount=10, settled_by="alice",
        )

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        assert "Paid automatically from Alice's wallet: 20 tix" in text
        assert "Settled: 10 tix" in text
        assert "Nothing left to settle" in text

    @pytest.mark.asyncio
    async def test_partially_paid_wallet_only(self, test_db):
        await self._seed_draft("s1", "g1", "alice", "bob", 50)
        await _settle("g1", "alice", "bob", 20, "wallet", "settle-1")

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        assert "Paid from wallet: 20 tix" in text
        assert "Still owed: 30 tix" in text
        assert "Settled" not in text

    @pytest.mark.asyncio
    async def test_partially_paid_external_only(self, test_db):
        await self._seed_draft("s1", "g1", "alice", "bob", 50)
        await create_settlement(
            guild_id="g1", payer_id="alice", payee_id="bob",
            amount=20, settled_by="alice",
        )

        lines, _ = await get_formatted_bet_outcomes(
            "s1", {"alice": "Alice", "bob": "Bob"}, winning_team_ids=["bob"],
        )
        text = self._text(lines)

        assert "Settled: 20 tix" in text
        assert "Still owed: 30 tix" in text
        assert "wallet" not in text.lower()
