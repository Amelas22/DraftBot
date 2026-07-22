"""
Unit tests for debt_service
"""
import pytest
import pytest_asyncio
import tempfile
import os
from datetime import datetime
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select

from models.debt_ledger import DebtLedger
from services.debt_service import (
    create_ledger_entries,
    get_balance_with,
    get_all_balances_for,
    get_entries_since_last_settlement,
    create_settlement,
    create_debt_entries_from_stakes,
    adjust_debt,
    get_guild_debt_stats,
    get_debt_history
)
from models.stake import StakeInfo
from models.stake_pairing import StakePairing


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database"""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    AsyncSessionLocal.configure(bind=engine)

    yield engine

    await engine.dispose()
    os.unlink(temp_db.name)


class TestCreateLedgerEntries:
    """Tests for create_ledger_entries function"""

    @pytest.mark.asyncio
    async def test_creates_two_entries(self, test_db):
        """Test that create_ledger_entries creates exactly two entries"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        assert debtor_entry is not None
        assert creditor_entry is not None
        assert debtor_entry.id != creditor_entry.id

    @pytest.mark.asyncio
    async def test_debtor_entry_has_negative_amount(self, test_db):
        """Test that debtor's entry has negative amount (they owe)"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        assert debtor_entry.amount == -30
        assert debtor_entry.player_id == "alice"
        assert debtor_entry.counterparty_id == "bob"

    @pytest.mark.asyncio
    async def test_creditor_entry_has_positive_amount(self, test_db):
        """Test that creditor's entry has positive amount (they are owed)"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        assert creditor_entry.amount == 30
        assert creditor_entry.player_id == "bob"
        assert creditor_entry.counterparty_id == "alice"

    @pytest.mark.asyncio
    async def test_entries_share_source_info(self, test_db):
        """Test that both entries have the same source information"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456",
            notes="Draft #456 bet outcome"
        )

        # Both entries should have same source info
        assert debtor_entry.source_type == creditor_entry.source_type == "draft"
        assert debtor_entry.source_id == creditor_entry.source_id == "session_456"
        assert debtor_entry.notes == creditor_entry.notes == "Draft #456 bet outcome"
        assert debtor_entry.guild_id == creditor_entry.guild_id == "guild_123"

    @pytest.mark.asyncio
    async def test_entries_persisted_to_database(self, test_db):
        """Test that entries are actually persisted to the database"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        # Query the database directly
        async with AsyncSessionLocal() as session:
            query = select(DebtLedger).where(DebtLedger.source_id == "session_456")
            result = await session.execute(query)
            entries = result.scalars().all()

            assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_created_at_is_set(self, test_db):
        """Test that created_at timestamp is automatically set"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        assert debtor_entry.created_at is not None
        assert creditor_entry.created_at is not None
        assert isinstance(debtor_entry.created_at, datetime)

    @pytest.mark.asyncio
    async def test_created_by_is_optional(self, test_db):
        """Test that created_by is optional"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        assert debtor_entry.created_by is None

    @pytest.mark.asyncio
    async def test_created_by_when_provided(self, test_db):
        """Test that created_by is stored when provided"""
        debtor_entry, creditor_entry = await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456",
            created_by="system"
        )

        assert debtor_entry.created_by == "system"
        assert creditor_entry.created_by == "system"

    @pytest.mark.asyncio
    async def test_rejects_zero_amount(self, test_db):
        """Test that zero amount raises an error"""
        with pytest.raises(ValueError, match="Amount must be positive"):
            await create_ledger_entries(
                guild_id="guild_123",
                debtor_id="alice",
                creditor_id="bob",
                amount=0,
                source_type="draft",
                source_id="session_456"
            )

    @pytest.mark.asyncio
    async def test_rejects_negative_amount(self, test_db):
        """Test that negative amount raises an error"""
        with pytest.raises(ValueError, match="Amount must be positive"):
            await create_ledger_entries(
                guild_id="guild_123",
                debtor_id="alice",
                creditor_id="bob",
                amount=-30,
                source_type="draft",
                source_id="session_456"
            )


class TestGetBalanceWith:
    """Tests for get_balance_with function"""

    @pytest.mark.asyncio
    async def test_returns_zero_for_no_entries(self, test_db):
        """Test that balance is 0 when no entries exist"""
        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert balance == 0

    @pytest.mark.asyncio
    async def test_returns_negative_for_debtor(self, test_db):
        """Test that debtor's balance is negative (they owe)"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert balance == -30

    @pytest.mark.asyncio
    async def test_returns_positive_for_creditor(self, test_db):
        """Test that creditor's balance is positive (they are owed)"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_456"
        )

        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="bob",
            counterparty_id="alice"
        )

        assert balance == 30

    @pytest.mark.asyncio
    async def test_sums_multiple_entries(self, test_db):
        """Test that multiple entries are summed correctly"""
        # Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Alice owes Bob another 20
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=20,
            source_type="draft",
            source_id="session_2"
        )

        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert balance == -50  # -30 + -20

    @pytest.mark.asyncio
    async def test_nets_opposite_debts(self, test_db):
        """Test that debts in opposite directions net out"""
        # Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Bob owes Alice 50
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="bob",
            creditor_id="alice",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        # From Alice's perspective: -30 + 50 = +20 (Bob owes her net)
        alice_balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )
        assert alice_balance == 20

        # From Bob's perspective: +30 - 50 = -20 (he owes Alice net)
        bob_balance = await get_balance_with(
            guild_id="guild_123",
            player_id="bob",
            counterparty_id="alice"
        )
        assert bob_balance == -20

    @pytest.mark.asyncio
    async def test_isolates_by_guild(self, test_db):
        """Test that balances are isolated per guild"""
        await create_ledger_entries(
            guild_id="guild_1",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        await create_ledger_entries(
            guild_id="guild_2",
            debtor_id="alice",
            creditor_id="bob",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        balance_guild_1 = await get_balance_with(
            guild_id="guild_1",
            player_id="alice",
            counterparty_id="bob"
        )
        assert balance_guild_1 == -30

        balance_guild_2 = await get_balance_with(
            guild_id="guild_2",
            player_id="alice",
            counterparty_id="bob"
        )
        assert balance_guild_2 == -50


class TestGetAllBalancesFor:
    """Tests for get_all_balances_for function"""

    @pytest.mark.asyncio
    async def test_returns_empty_dict_for_no_entries(self, test_db):
        """Test that empty dict is returned when no entries exist"""
        balances = await get_all_balances_for(
            guild_id="guild_123",
            player_id="alice"
        )

        assert balances == {}

    @pytest.mark.asyncio
    async def test_returns_single_counterparty(self, test_db):
        """Test with a single counterparty"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        balances = await get_all_balances_for(
            guild_id="guild_123",
            player_id="alice"
        )

        assert balances == {"bob": -30}

    @pytest.mark.asyncio
    async def test_returns_multiple_counterparties(self, test_db):
        """Test with multiple counterparties"""
        # Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Alice owes Charlie 20
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="charlie",
            amount=20,
            source_type="draft",
            source_id="session_2"
        )

        # Dave owes Alice 50
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="dave",
            creditor_id="alice",
            amount=50,
            source_type="draft",
            source_id="session_3"
        )

        balances = await get_all_balances_for(
            guild_id="guild_123",
            player_id="alice"
        )

        assert balances == {
            "bob": -30,      # Alice owes Bob
            "charlie": -20,  # Alice owes Charlie
            "dave": 50       # Dave owes Alice
        }

    @pytest.mark.asyncio
    async def test_excludes_zero_balances(self, test_db):
        """Test that zero balances are excluded"""
        # Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Bob owes Alice 30 (nets to zero)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="bob",
            creditor_id="alice",
            amount=30,
            source_type="draft",
            source_id="session_2"
        )

        # Alice owes Charlie 20 (non-zero)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="charlie",
            amount=20,
            source_type="draft",
            source_id="session_3"
        )

        balances = await get_all_balances_for(
            guild_id="guild_123",
            player_id="alice"
        )

        # Bob should be excluded (net zero), only Charlie remains
        assert balances == {"charlie": -20}

    @pytest.mark.asyncio
    async def test_isolates_by_guild(self, test_db):
        """Test that balances are isolated per guild"""
        await create_ledger_entries(
            guild_id="guild_1",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        await create_ledger_entries(
            guild_id="guild_2",
            debtor_id="alice",
            creditor_id="charlie",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        balances_guild_1 = await get_all_balances_for(
            guild_id="guild_1",
            player_id="alice"
        )
        assert balances_guild_1 == {"bob": -30}

        balances_guild_2 = await get_all_balances_for(
            guild_id="guild_2",
            player_id="alice"
        )
        assert balances_guild_2 == {"charlie": -50}

    @pytest.mark.asyncio
    async def test_nets_multiple_entries_same_counterparty(self, test_db):
        """Test that multiple entries with same counterparty are netted"""
        # Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Bob owes Alice 50
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="bob",
            creditor_id="alice",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        balances = await get_all_balances_for(
            guild_id="guild_123",
            player_id="alice"
        )

        # Net: -30 + 50 = +20 (Bob owes Alice 20)
        assert balances == {"bob": 20}


class TestGetEntriesSinceLastSettlement:
    """Tests for get_entries_since_last_settlement function"""

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_no_entries(self, test_db):
        """Test that empty list is returned when no entries exist"""
        entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert entries == []

    @pytest.mark.asyncio
    async def test_returns_all_entries_when_no_settlement(self, test_db):
        """Test that all entries are returned when no settlement exists"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=20,
            source_type="draft",
            source_id="session_2"
        )

        entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert len(entries) == 2
        assert entries[0].amount == -30  # First entry
        assert entries[1].amount == -20  # Second entry

    @pytest.mark.asyncio
    async def test_excludes_entries_before_settlement(self, test_db):
        """Test that entries before last settlement are excluded"""
        # First draft debt (before settlement)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Settlement (zeros out balance)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="bob",  # Settlement reverses the debt
            creditor_id="alice",
            amount=30,
            source_type="settlement",
            source_id="settlement_uuid_1"
        )

        # New draft debt (after settlement)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        # Should only include the new draft debt
        assert len(entries) == 1
        assert entries[0].amount == -50
        assert entries[0].source_type == "draft"
        assert entries[0].source_id == "session_2"

    @pytest.mark.asyncio
    async def test_returns_entries_in_chronological_order(self, test_db):
        """Test that entries are returned in chronological order (oldest first)"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=10,
            source_type="draft",
            source_id="session_1"
        )

        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="bob",
            creditor_id="alice",
            amount=20,
            source_type="draft",
            source_id="session_2"
        )

        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_3"
        )

        entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert len(entries) == 3
        assert entries[0].amount == -10  # First (oldest)
        assert entries[1].amount == 20   # Second (from bob owing alice)
        assert entries[2].amount == -30  # Third (newest)

    @pytest.mark.asyncio
    async def test_isolates_by_guild(self, test_db):
        """Test that entries are isolated per guild"""
        await create_ledger_entries(
            guild_id="guild_1",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        await create_ledger_entries(
            guild_id="guild_2",
            debtor_id="alice",
            creditor_id="bob",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        entries_guild_1 = await get_entries_since_last_settlement(
            guild_id="guild_1",
            player_id="alice",
            counterparty_id="bob"
        )
        assert len(entries_guild_1) == 1
        assert entries_guild_1[0].amount == -30

        entries_guild_2 = await get_entries_since_last_settlement(
            guild_id="guild_2",
            player_id="alice",
            counterparty_id="bob"
        )
        assert len(entries_guild_2) == 1
        assert entries_guild_2[0].amount == -50

    @pytest.mark.asyncio
    async def test_only_considers_player_perspective(self, test_db):
        """Test that only entries from player's perspective are returned"""
        # This creates TWO entries (one for each player)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # From Alice's perspective
        alice_entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )
        assert len(alice_entries) == 1
        assert alice_entries[0].amount == -30  # Alice owes

        # From Bob's perspective
        bob_entries = await get_entries_since_last_settlement(
            guild_id="guild_123",
            player_id="bob",
            counterparty_id="alice"
        )
        assert len(bob_entries) == 1
        assert bob_entries[0].amount == 30  # Bob is owed


class TestCreateSettlement:
    """Tests for create_settlement function"""

    @pytest.mark.asyncio
    async def test_creates_two_entries(self, test_db):
        """Test that settlement creates exactly two entries"""
        # Setup: Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payer_entry is not None
        assert payee_entry is not None
        assert payer_entry.id != payee_entry.id

    @pytest.mark.asyncio
    async def test_payer_entry_is_positive(self, test_db):
        """Test that payer's entry is positive (reduces their debt)"""
        # Setup: Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payer_entry.amount == 30  # Positive: reduces Alice's debt
        assert payer_entry.player_id == "alice"
        assert payer_entry.counterparty_id == "bob"

    @pytest.mark.asyncio
    async def test_payee_entry_is_negative(self, test_db):
        """Test that payee's entry is negative (reduces their credit)"""
        # Setup: Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payee_entry.amount == -30  # Negative: reduces Bob's credit
        assert payee_entry.player_id == "bob"
        assert payee_entry.counterparty_id == "alice"

    @pytest.mark.asyncio
    async def test_full_settlement_zeros_balance(self, test_db):
        """Test that full settlement zeros out the balance"""
        # Setup: Alice owes Bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert balance == 0

    @pytest.mark.asyncio
    async def test_partial_settlement_leaves_remainder(self, test_db):
        """Test that partial settlement leaves remaining balance"""
        # Setup: Alice owes Bob 50
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=50,
            source_type="draft",
            source_id="session_1"
        )

        # Alice pays 30 (partial)
        await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )

        assert balance == -20  # Alice still owes 20

    @pytest.mark.asyncio
    async def test_entries_have_settlement_source_type(self, test_db):
        """Test that entries have source_type='settlement'"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payer_entry.source_type == "settlement"
        assert payee_entry.source_type == "settlement"

    @pytest.mark.asyncio
    async def test_entries_share_uuid_source_id(self, test_db):
        """Test that both entries share the same UUID source_id"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payer_entry.source_id == payee_entry.source_id
        assert payer_entry.source_id is not None
        # Should look like a UUID (has hyphens)
        assert '-' in payer_entry.source_id

    @pytest.mark.asyncio
    async def test_stores_settled_by(self, test_db):
        """Test that settled_by is stored in created_by"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert payer_entry.created_by == "alice"
        assert payee_entry.created_by == "alice"

    @pytest.mark.asyncio
    async def test_stores_notes_with_amount(self, test_db):
        """Test that notes include the confirmed amount"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        payer_entry, payee_entry = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice"
        )

        assert "30" in payer_entry.notes
        assert "30" in payee_entry.notes

    @pytest.mark.asyncio
    async def test_rejects_zero_amount(self, test_db):
        """Test that zero amount raises an error"""
        with pytest.raises(ValueError, match="Amount must be positive"):
            await create_settlement(
                guild_id="guild_123",
                payer_id="alice",
                payee_id="bob",
                amount=0,
                settled_by="alice"
            )

    @pytest.mark.asyncio
    async def test_rejects_negative_amount(self, test_db):
        """Test that negative amount raises an error"""
        with pytest.raises(ValueError, match="Amount must be positive"):
            await create_settlement(
                guild_id="guild_123",
                payer_id="alice",
                payee_id="bob",
                amount=-30,
                settled_by="alice"
            )

    @pytest.mark.asyncio
    async def test_is_idempotent_with_settlement_id(self, test_db):
        """Test that providing the same settlement_id returns existing entries"""
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # First call creates the settlement
        payer_entry1, payee_entry1 = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice",
            settlement_id="test_settlement_123"
        )

        # Second call with same settlement_id returns existing entries
        payer_entry2, payee_entry2 = await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=30,
            settled_by="alice",
            settlement_id="test_settlement_123"
        )

        # Should be the same entries
        assert payer_entry1.id == payer_entry2.id
        assert payee_entry1.id == payee_entry2.id

        # Balance should only reflect one settlement
        balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="bob"
        )
        assert balance == 0  # 30 debt - 30 settlement = 0


class TestCreateDebtEntriesFromStakes:
    """Tests for create_debt_entries_from_stakes function"""

    @pytest_asyncio.fixture
    async def setup_stakes(self, test_db):
        """Helper fixture to create stake records"""
        async with AsyncSessionLocal() as session:
            # Create stakes for a 4-player draft
            # Team A (winners): alice, bob
            # Team B (losers): charlie, dave
            # Stakes: alice vs charlie (30), bob vs dave (20)

            stakes = [
                # Alice's stake with Charlie
                StakeInfo(
                    session_id="session_123",
                    player_id="alice",
                    max_stake=50,
                    assigned_stake=30,
                    opponent_id="charlie"
                ),
                # Charlie's stake with Alice (mirror)
                StakeInfo(
                    session_id="session_123",
                    player_id="charlie",
                    max_stake=50,
                    assigned_stake=30,
                    opponent_id="alice"
                ),
                # Bob's stake with Dave
                StakeInfo(
                    session_id="session_123",
                    player_id="bob",
                    max_stake=40,
                    assigned_stake=20,
                    opponent_id="dave"
                ),
                # Dave's stake with Bob (mirror)
                StakeInfo(
                    session_id="session_123",
                    player_id="dave",
                    max_stake=40,
                    assigned_stake=20,
                    opponent_id="bob"
                ),
            ]

            for stake in stakes:
                session.add(stake)

            # Create corresponding StakePairing records (output data queried by functions under test)
            pairings = [
                StakePairing(session_id="session_123", player_a_id="alice", player_b_id="charlie", amount=30),
                StakePairing(session_id="session_123", player_a_id="bob", player_b_id="dave", amount=20),
            ]
            for pairing in pairings:
                session.add(pairing)

            await session.commit()

        return test_db

    @pytest.mark.asyncio
    async def test_creates_debt_entries_for_stakes(self, setup_stakes):
        """Test that debt entries are created for stake outcomes"""
        # Team A wins (alice, bob)
        winning_team = ["alice", "bob"]

        debts = await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_123",
            winning_team_ids=winning_team
        )

        # Should create 2 debts: charlie owes alice 30, dave owes bob 20
        assert len(debts) == 2

        # Verify debts created (order may vary)
        debt_set = {(d[0], d[1], d[2]) for d in debts}
        assert ("charlie", "alice", 30) in debt_set
        assert ("dave", "bob", 20) in debt_set

    @pytest.mark.asyncio
    async def test_creates_correct_ledger_entries(self, setup_stakes):
        """Test that ledger entries have correct amounts"""
        winning_team = ["alice", "bob"]

        await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_123",
            winning_team_ids=winning_team
        )

        # Check Charlie's balance with Alice (should owe 30)
        charlie_balance = await get_balance_with(
            guild_id="guild_123",
            player_id="charlie",
            counterparty_id="alice"
        )
        assert charlie_balance == -30

        # Check Alice's balance with Charlie (should be owed 30)
        alice_balance = await get_balance_with(
            guild_id="guild_123",
            player_id="alice",
            counterparty_id="charlie"
        )
        assert alice_balance == 30

    @pytest.mark.asyncio
    async def test_skips_same_team_stakes(self, test_db):
        """Test that stakes between players on same team are skipped"""
        async with AsyncSessionLocal() as session:
            # Both alice and bob on winning team, stake between them
            stakes = [
                StakeInfo(
                    session_id="session_456",
                    player_id="alice",
                    max_stake=50,
                    assigned_stake=30,
                    opponent_id="bob"
                ),
                StakeInfo(
                    session_id="session_456",
                    player_id="bob",
                    max_stake=50,
                    assigned_stake=30,
                    opponent_id="alice"
                ),
            ]
            for stake in stakes:
                session.add(stake)
            await session.commit()

        # Both on winning team
        winning_team = ["alice", "bob"]

        debts = await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_456",
            winning_team_ids=winning_team
        )

        # No debts should be created (same team)
        assert len(debts) == 0

    @pytest.mark.asyncio
    async def test_is_idempotent(self, setup_stakes):
        """Test that calling twice doesn't create duplicate entries"""
        winning_team = ["alice", "bob"]

        # First call
        debts1 = await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_123",
            winning_team_ids=winning_team
        )
        assert len(debts1) == 2

        # Second call should return empty (already exists)
        debts2 = await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_123",
            winning_team_ids=winning_team
        )
        assert len(debts2) == 0

        # Verify only 2 pairs exist (4 entries total: 2 per pair)
        async with AsyncSessionLocal() as session:
            query = select(DebtLedger).where(DebtLedger.source_id == "session_123")
            result = await session.execute(query)
            entries = result.scalars().all()
            assert len(entries) == 4  # 2 pairs * 2 entries each

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_stakes(self, test_db):
        """Test that empty list is returned when no stakes exist"""
        debts = await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="nonexistent_session",
            winning_team_ids=["alice", "bob"]
        )

        assert debts == []

    @pytest.mark.asyncio
    async def test_source_type_is_draft(self, setup_stakes):
        """Test that entries have source_type='draft'"""
        winning_team = ["alice", "bob"]

        await create_debt_entries_from_stakes(
            guild_id="guild_123",
            session_id="session_123",
            winning_team_ids=winning_team
        )

        async with AsyncSessionLocal() as session:
            query = select(DebtLedger).where(DebtLedger.source_id == "session_123")
            result = await session.execute(query)
            entries = result.scalars().all()

            for entry in entries:
                assert entry.source_type == "draft"
                assert entry.source_id == "session_123"


class TestAdjustDebt:
    """Tests for adjust_debt function (admin debt management)"""

    @pytest.mark.asyncio
    async def test_positive_amount_creates_debt(self, test_db):
        """Test that positive amount creates debt (player1 owes player2)"""
        new_balance = await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=30,
            notes="Admin test debt",
            created_by="admin_user"
        )

        # player1 (alice) owes player2 (bob) 30
        assert new_balance == -30

    @pytest.mark.asyncio
    async def test_negative_amount_reduces_debt(self, test_db):
        """Test that negative amount reduces debt (forgiveness)"""
        # First create debt
        await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=50,
            notes="Initial debt",
            created_by="admin_user"
        )

        # Then forgive some
        new_balance = await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=-30,
            notes="Partial forgiveness",
            created_by="admin_user"
        )

        # alice now owes bob only 20
        assert new_balance == -20

    @pytest.mark.asyncio
    async def test_creates_ledger_entries_with_admin_source(self, test_db):
        """Test that entries have source_type='admin'"""
        await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=30,
            notes="Admin test",
            created_by="admin_user"
        )

        async with AsyncSessionLocal() as session:
            query = select(DebtLedger).where(
                DebtLedger.guild_id == "guild_123",
                DebtLedger.source_type == "admin"
            )
            result = await session.execute(query)
            entries = result.scalars().all()

            assert len(entries) == 2
            for entry in entries:
                assert entry.source_type == "admin"
                assert entry.notes == "Admin test"
                assert entry.created_by == "admin_user"

    @pytest.mark.asyncio
    async def test_rejects_zero_amount(self, test_db):
        """Test that zero amount raises an error"""
        with pytest.raises(ValueError, match="Amount cannot be zero"):
            await adjust_debt(
                guild_id="guild_123",
                player1_id="alice",
                player2_id="bob",
                amount=0,
                notes="Invalid",
                created_by="admin_user"
            )

    @pytest.mark.asyncio
    async def test_negative_amount_can_flip_debt_direction(self, test_db):
        """Test that negative amount can flip who owes whom"""
        # alice owes bob 30
        await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=30,
            notes="Initial",
            created_by="admin"
        )

        # Forgive 50 (more than owed), bob now owes alice 20
        new_balance = await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=-50,
            notes="Overpayment",
            created_by="admin"
        )

        # alice's balance with bob is now +20 (bob owes her)
        assert new_balance == 20


class TestGetGuildDebtStats:
    """Tests for get_guild_debt_stats function"""

    @pytest.mark.asyncio
    async def test_returns_zero_stats_for_no_debts(self, test_db):
        """Test that stats are zero when no debts exist"""
        stats = await get_guild_debt_stats("guild_123", "all_time")

        assert stats['total_debt'] == 0
        assert stats['num_debtors'] == 0
        assert stats['num_creditors'] == 0
        assert stats['largest_debt'] is None
        assert stats['avg_debt_per_debtor'] == 0

    @pytest.mark.asyncio
    async def test_calculates_total_debt(self, test_db):
        """Test that total debt is calculated correctly"""
        # alice owes bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # charlie owes dave 20
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="charlie",
            creditor_id="dave",
            amount=20,
            source_type="draft",
            source_id="session_2"
        )

        stats = await get_guild_debt_stats("guild_123", "all_time")

        assert stats['total_debt'] == 50  # 30 + 20
        assert stats['num_debtors'] == 2  # alice, charlie
        assert stats['num_creditors'] == 2  # bob, dave

    @pytest.mark.asyncio
    async def test_identifies_largest_debt(self, test_db):
        """Test that largest debt is identified"""
        # alice owes bob 30
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # charlie owes dave 50 (largest)
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="charlie",
            creditor_id="dave",
            amount=50,
            source_type="draft",
            source_id="session_2"
        )

        stats = await get_guild_debt_stats("guild_123", "all_time")

        assert stats['largest_debt'] == ("charlie", "dave", 50)

    @pytest.mark.asyncio
    async def test_counts_entries_by_source_type(self, test_db):
        """Test that debt_by_source breakdown works"""
        # Create a draft debt
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Create an admin debt
        await adjust_debt(
            guild_id="guild_123",
            player1_id="charlie",
            player2_id="dave",
            amount=20,
            notes="Admin debt",
            created_by="admin"
        )

        stats = await get_guild_debt_stats("guild_123", "all_time")

        # 2 entries per debt (double-entry)
        assert stats['debt_by_source']['draft'] == 2
        assert stats['debt_by_source']['admin'] == 2


class TestGetDebtHistory:
    """Tests for get_debt_history function"""

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_entries(self, test_db):
        """Test that empty list is returned when no entries exist"""
        entries = await get_debt_history("guild_123")

        assert entries == []

    @pytest.mark.asyncio
    async def test_returns_all_entry_types(self, test_db):
        """Test that all entry types are returned (draft, settlement, admin)"""
        # Create a draft debt
        await create_ledger_entries(
            guild_id="guild_123",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="session_1"
        )

        # Create an admin debt
        await adjust_debt(
            guild_id="guild_123",
            player1_id="charlie",
            player2_id="dave",
            amount=20,
            notes="Admin adjustment",
            created_by="admin"
        )

        # Create a settlement
        await create_settlement(
            guild_id="guild_123",
            payer_id="alice",
            payee_id="bob",
            amount=10,
            settled_by="alice"
        )

        entries = await get_debt_history("guild_123")

        # Should have 6 entries: 2 from draft + 2 from admin + 2 from settlement
        assert len(entries) == 6
        source_types = {entry.source_type for entry in entries}
        assert "draft" in source_types
        assert "admin" in source_types
        assert "settlement" in source_types

    @pytest.mark.asyncio
    async def test_filters_by_player(self, test_db):
        """Test that player filter works"""
        # Admin debt: alice <-> bob
        await adjust_debt(
            guild_id="guild_123",
            player1_id="alice",
            player2_id="bob",
            amount=30,
            notes="Alice/Bob debt",
            created_by="admin"
        )

        # Admin debt: charlie <-> dave
        await adjust_debt(
            guild_id="guild_123",
            player1_id="charlie",
            player2_id="dave",
            amount=20,
            notes="Charlie/Dave debt",
            created_by="admin"
        )

        # Filter for alice
        entries = await get_debt_history("guild_123", player_id="alice")

        # Should only see entries involving alice (2: her entry + bob's entry)
        assert len(entries) == 2
        player_ids = {entry.player_id for entry in entries}
        assert "alice" in player_ids or "bob" in player_ids

    @pytest.mark.asyncio
    async def test_respects_limit(self, test_db):
        """Test that limit parameter works"""
        # Create multiple admin debts
        for i in range(5):
            await adjust_debt(
                guild_id="guild_123",
                player1_id="alice",
                player2_id="bob",
                amount=10 * (i + 1),
                notes=f"Debt {i}",
                created_by="admin"
            )

        # Each adjust_debt creates 2 entries, so 5 calls = 10 entries
        # Request limit of 5
        entries = await get_debt_history("guild_123", limit=5)

        assert len(entries) == 5

    @pytest.mark.asyncio
    async def test_returns_newest_first(self, test_db):
        """Test that entries are returned in reverse chronological order"""
        # Create 3 admin debts
        for i in range(3):
            await adjust_debt(
                guild_id="guild_123",
                player1_id="alice",
                player2_id="bob",
                amount=10 * (i + 1),
                notes=f"Debt {i}",
                created_by="admin"
            )

        entries = await get_debt_history("guild_123", limit=10)

        # Verify they're in descending order by creation time
        for i in range(len(entries) - 1):
            assert entries[i].created_at >= entries[i + 1].created_at


from unittest.mock import patch
from services.debt_service import get_top_net_creditors, get_most_outstanding_creditors, get_most_involved_players


class TestTopNetCreditors:
    """Tests for the Most Outstanding leaderboard query."""

    async def _owe(self, guild, debtor, creditor, amount):
        await create_ledger_entries(
            guild_id=guild, debtor_id=debtor, creditor_id=creditor,
            amount=amount, source_type="draft", source_id=f"s-{debtor}-{creditor}",
        )

    @pytest.mark.asyncio
    async def test_ranks_net_creditors_desc_and_excludes_non_creditors(self, test_db):
        g = "g1"
        # bob owes alice 120; carol owes alice 30  -> alice net +150
        await self._owe(g, "bob", "alice", 120)
        await self._owe(g, "carol", "alice", 30)
        # dave owes bob 200 -> bob net (-120 + 200) = +80
        await self._owe(g, "dave", "bob", 200)
        # carol net -30 (excluded), dave net -200 (excluded)

        result = await get_top_net_creditors(g, limit=3)

        assert result == [("alice", 150), ("bob", 80)]

    @pytest.mark.asyncio
    async def test_excludes_net_zero_players(self, test_db):
        g = "g2"
        # alice and bob owe each other equally -> both net 0
        await self._owe(g, "bob", "alice", 50)
        await self._owe(g, "alice", "bob", 50)

        result = await get_top_net_creditors(g, limit=3)

        assert result == []

    @pytest.mark.asyncio
    async def test_respects_limit(self, test_db):
        g = "g3"
        for i, amt in enumerate([10, 40, 30, 20]):
            await self._owe(g, f"debtor{i}", f"cred{i}", amt)
        # nets: cred1=40, cred2=30, cred3=20, cred0=10 -> top 2 = cred1, cred2
        result = await get_top_net_creditors(g, limit=2)

        assert result == [("cred1", 40), ("cred2", 30)]

    @pytest.mark.asyncio
    async def test_scoped_to_guild(self, test_db):
        await self._owe("gA", "bob", "alice", 99)
        await self._owe("gB", "dan", "carol", 5)

        result = await get_top_net_creditors("gA", limit=3)

        assert result == [("alice", 99)]

    @pytest.mark.asyncio
    async def test_gate_returns_empty_on_non_money_server(self, test_db):
        await self._owe("gX", "bob", "alice", 99)
        with patch("config.is_money_server", return_value=False):
            result = await get_most_outstanding_creditors("gX", limit=3)
        assert result == []

    @pytest.mark.asyncio
    async def test_gate_delegates_on_money_server(self, test_db):
        # alice involved in 2 debts, so she tops the involvement ranking
        await self._owe("gY", "alice", "bob", 40)
        await self._owe("gY", "alice", "carol", 60)
        with patch("config.is_money_server", return_value=True):
            result = await get_most_outstanding_creditors("gY", limit=3)
        assert result[0] == ("alice", 2)


class TestMostInvolvedPlayers:
    """Tests for the involvement-count leaderboard query."""

    async def _owe(self, guild, debtor, creditor, amount):
        await create_ledger_entries(
            guild_id=guild, debtor_id=debtor, creditor_id=creditor,
            amount=amount, source_type="draft", source_id=f"s-{debtor}-{creditor}-{amount}",
        )

    @pytest.mark.asyncio
    async def test_counts_both_directions(self, test_db):
        g = "mi1"
        await self._owe(g, "alice", "bob", 10)     # alice owes bob
        await self._owe(g, "alice", "carol", 20)   # alice owes carol
        await self._owe(g, "dave", "alice", 30)    # dave owes alice
        result = await get_most_involved_players(g, limit=5)
        counts = dict(result)
        assert counts["alice"] == 3   # 2 owing + 1 owed
        assert counts["bob"] == 1
        assert counts["carol"] == 1
        assert counts["dave"] == 1

    @pytest.mark.asyncio
    async def test_excludes_net_zero_relationships(self, test_db):
        g = "mi2"
        await self._owe(g, "alice", "bob", 50)
        await self._owe(g, "bob", "alice", 50)   # cancels out
        result = await get_most_involved_players(g, limit=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_orders_by_count_then_amount(self, test_db):
        g = "mi3"
        await self._owe(g, "alice", "x", 5)
        await self._owe(g, "alice", "y", 5)      # alice: 2 debts
        await self._owe(g, "bob", "z", 999)      # bob: 1 debt, big
        result = await get_most_involved_players(g, limit=5)
        assert result[0] == ("alice", 2)          # count beats amount
        assert ("bob", 1) in result

    @pytest.mark.asyncio
    async def test_amount_tiebreak_on_equal_counts(self, test_db):
        g = "mi4"
        await self._owe(g, "alice", "p", 10)
        await self._owe(g, "bob", "q", 500)       # both 1 debt; bob larger
        result = await get_most_involved_players(g, limit=5)
        order = [pid for pid, _ in result]
        assert order.index("bob") < order.index("alice")

    @pytest.mark.asyncio
    async def test_respects_limit_and_guild(self, test_db):
        g = "mi5"
        await self._owe(g, "a", "b", 10)
        await self._owe(g, "a", "c", 10)          # a: 2
        await self._owe(g, "d", "e", 10)
        await self._owe("other", "z", "y", 99)    # different guild
        result = await get_most_involved_players(g, limit=1)
        assert result == [("a", 2)]
