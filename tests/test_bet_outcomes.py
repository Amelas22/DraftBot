"""
Unit tests for bet outcomes display with pre-existing and net debt
"""
import pytest
import pytest_asyncio
import tempfile
import os
from database.models_base import Base
from database.db_session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import create_async_engine

from utils import get_formatted_bet_outcomes
from models.draft_session import DraftSession
from models.stake import StakeInfo
from models.debt_ledger import DebtLedger
from services.debt_service import create_ledger_entries


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


class TestBetOutcomesDisplay:
    """Tests for bet outcomes display with pre-existing and net debt"""

    @pytest.mark.asyncio
    async def test_first_bet_between_players(self, test_db):
        """Case 1: No pre-existing debt"""
        # Setup: Create draft session with stakes
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Create draft session
                draft = DraftSession(
                    session_id="test_session_1",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                # Create stakes (Alice on team A, Bob on team B)
                stake_alice = StakeInfo(
                    session_id="test_session_1",
                    player_id="alice",
                    max_stake=30,
                    opponent_id="bob",
                    assigned_stake=30
                )
                stake_bob = StakeInfo(
                    session_id="test_session_1",
                    player_id="bob",
                    max_stake=30,
                    opponent_id="alice",
                    assigned_stake=30
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        # Call the function (Bob's team wins, so Alice loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_1",
            sign_ups,
            ["bob"]  # winning_team_ids
        )

        # Assert: Output shows "Pre-existing: 0 tix"
        assert len(outcome_lines) == 1
        outcome = outcome_lines[0]
        assert "Alice owes Bob:" in outcome
        assert "This draft: 30 tix" in outcome
        assert "Pre-existing: 0 tix" in outcome
        assert "Net total: 30 tix" in outcome

    @pytest.mark.asyncio
    async def test_debt_increases_same_direction(self, test_db):
        """Case 2: A owes B 10, A loses 20 more"""
        # Setup: Create pre-existing debt
        await create_ledger_entries(
            guild_id="test_guild",
            debtor_id="alice",
            creditor_id="bob",
            amount=10,
            source_type="draft",
            source_id="previous_session"
        )

        # Create new draft session with stakes
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_2",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                stake_alice = StakeInfo(
                    session_id="test_session_2",
                    player_id="alice",
                    max_stake=20,
                    opponent_id="bob",
                    assigned_stake=20
                )
                stake_bob = StakeInfo(
                    session_id="test_session_2",
                    player_id="bob",
                    max_stake=20,
                    opponent_id="alice",
                    assigned_stake=20
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        # Call the function (Bob wins again, Alice loses again)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_2",
            sign_ups,
            ["bob"]
        )

        # Assert: Shows previous 10, this draft 20, net 30
        assert len(outcome_lines) == 1
        outcome = outcome_lines[0]
        assert "Alice owes Bob:" in outcome
        assert "This draft: 20 tix" in outcome
        assert "Pre-existing: 10 tix" in outcome
        assert "Net total: 30 tix" in outcome

    @pytest.mark.asyncio
    async def test_preexisting_debt_subsumes_new_debt(self, test_db):
        """Case 3: A owes B 50, B loses 10 to A"""
        # Setup: Create pre-existing debt (Alice owes Bob 50)
        await create_ledger_entries(
            guild_id="test_guild",
            debtor_id="alice",
            creditor_id="bob",
            amount=50,
            source_type="draft",
            source_id="previous_session"
        )

        # Create new draft session where Bob loses to Alice
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_3",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                stake_alice = StakeInfo(
                    session_id="test_session_3",
                    player_id="alice",
                    max_stake=10,
                    opponent_id="bob",
                    assigned_stake=10
                )
                stake_bob = StakeInfo(
                    session_id="test_session_3",
                    player_id="bob",
                    max_stake=10,
                    opponent_id="alice",
                    assigned_stake=10
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_3",
            sign_ups,
            ["alice"]  # Alice's team wins
        )

        # Assert: Shows "Alice owes Bob" with net 40 (50 - 10 = 40)
        assert len(outcome_lines) == 1
        outcome = outcome_lines[0]
        assert "Alice owes Bob:" in outcome
        assert "Bob lost 10 tix" in outcome
        assert "Alice owed 50 tix" in outcome
        assert "Alice owes 40 tix" in outcome
        assert "50 - 10" in outcome  # Shows the math

    @pytest.mark.asyncio
    async def test_debt_direction_inverts(self, test_db):
        """Case 4: A owes B 10, B loses 20 to A"""
        # Setup: Create pre-existing debt (Alice owes Bob 10)
        await create_ledger_entries(
            guild_id="test_guild",
            debtor_id="alice",
            creditor_id="bob",
            amount=10,
            source_type="draft",
            source_id="previous_session"
        )

        # Create new draft session where Bob loses 20 to Alice
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_4",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                stake_alice = StakeInfo(
                    session_id="test_session_4",
                    player_id="alice",
                    max_stake=20,
                    opponent_id="bob",
                    assigned_stake=20
                )
                stake_bob = StakeInfo(
                    session_id="test_session_4",
                    player_id="bob",
                    max_stake=20,
                    opponent_id="alice",
                    assigned_stake=20
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_4",
            sign_ups,
            ["alice"]  # Alice's team wins
        )

        # Assert: Shows "Bob owes Alice" with net 10 (20 - 10 = 10)
        assert len(outcome_lines) == 1
        outcome = outcome_lines[0]
        assert "Bob owes Alice:" in outcome
        assert "Bob lost 20 tix" in outcome
        assert "Bob was owed 10 tix" in outcome
        assert "Bob owes 10 tix" in outcome
        assert "20 - 10" in outcome  # Shows the math

    @pytest.mark.asyncio
    async def test_debts_cancel_exactly(self, test_db):
        """Case 5: A owes B 30, B loses exactly 30"""
        # Setup: Create pre-existing debt (Alice owes Bob 30)
        await create_ledger_entries(
            guild_id="test_guild",
            debtor_id="alice",
            creditor_id="bob",
            amount=30,
            source_type="draft",
            source_id="previous_session"
        )

        # Create new draft session where Bob loses exactly 30
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_5",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                stake_alice = StakeInfo(
                    session_id="test_session_5",
                    player_id="alice",
                    max_stake=30,
                    opponent_id="bob",
                    assigned_stake=30
                )
                stake_bob = StakeInfo(
                    session_id="test_session_5",
                    player_id="bob",
                    max_stake=30,
                    opponent_id="alice",
                    assigned_stake=30
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses 30)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_5",
            sign_ups,
            ["alice"]  # Alice's team wins
        )

        # Assert: Shows "0 tix (debts canceled!)"
        assert len(outcome_lines) == 1
        outcome = outcome_lines[0]
        assert "Debt between" in outcome
        assert "This draft: 30 tix" in outcome
        assert "Pre-existing: 30 tix" in outcome
        assert "0 tix (debts canceled!)" in outcome

    @pytest.mark.asyncio
    async def test_multiple_pairs_in_one_draft(self, test_db):
        """Test multiple stake pairs are all displayed correctly"""
        # Setup: Create draft with two pairs
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_6",
                    guild_id="test_guild",
                    team_a=["alice", "charlie"],
                    team_b=["bob", "dave"]
                )
                db_session.add(draft)

                # Alice vs Bob stake
                stake_alice = StakeInfo(
                    session_id="test_session_6",
                    player_id="alice",
                    max_stake=30,
                    opponent_id="bob",
                    assigned_stake=30
                )
                stake_bob = StakeInfo(
                    session_id="test_session_6",
                    player_id="bob",
                    max_stake=30,
                    opponent_id="alice",
                    assigned_stake=30
                )

                # Charlie vs Dave stake
                stake_charlie = StakeInfo(
                    session_id="test_session_6",
                    player_id="charlie",
                    max_stake=20,
                    opponent_id="dave",
                    assigned_stake=20
                )
                stake_dave = StakeInfo(
                    session_id="test_session_6",
                    player_id="dave",
                    max_stake=20,
                    opponent_id="charlie",
                    assigned_stake=20
                )

                db_session.add_all([stake_alice, stake_bob, stake_charlie, stake_dave])
                await db_session.commit()

        # Call the function (Bob and Dave win)
        sign_ups = {"alice": "Alice", "bob": "Bob", "charlie": "Charlie", "dave": "Dave"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_6",
            sign_ups,
            ["bob", "dave"]  # Team B wins
        )

        # Assert: All pairs shown
        assert len(outcome_lines) == 2

        # Check that both pairs are present
        outcomes_text = "\n".join(outcome_lines)
        assert "Alice owes Bob" in outcomes_text
        assert "Charlie owes Dave" in outcomes_text

    @pytest.mark.asyncio
    async def test_idempotency_after_debt_creation(self, test_db):
        """Test that calling get_formatted_bet_outcomes multiple times gives same result

        This verifies the fix for the bug where debt entries from THIS session
        were being counted as pre-existing debt on subsequent calls.
        """
        from services.debt_service import create_debt_entries_from_stakes

        # Setup: Create draft session with stakes
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                draft = DraftSession(
                    session_id="test_session_idempotent",
                    guild_id="test_guild",
                    team_a=["alice"],
                    team_b=["bob"]
                )
                db_session.add(draft)

                stake_alice = StakeInfo(
                    session_id="test_session_idempotent",
                    player_id="alice",
                    max_stake=30,
                    opponent_id="bob",
                    assigned_stake=30
                )
                stake_bob = StakeInfo(
                    session_id="test_session_idempotent",
                    player_id="bob",
                    max_stake=30,
                    opponent_id="alice",
                    assigned_stake=30
                )
                db_session.add_all([stake_alice, stake_bob])
                await db_session.commit()

        sign_ups = {"alice": "Alice", "bob": "Bob"}
        winning_team = ["bob"]

        # Call 1: Before debt entries are created
        outcome_lines_1, total_1 = await get_formatted_bet_outcomes(
            "test_session_idempotent",
            sign_ups,
            winning_team
        )

        # Create debt entries
        await create_debt_entries_from_stakes(
            guild_id="test_guild",
            session_id="test_session_idempotent",
            winning_team_ids=winning_team
        )

        # Call 2: After debt entries are created
        outcome_lines_2, total_2 = await get_formatted_bet_outcomes(
            "test_session_idempotent",
            sign_ups,
            winning_team
        )

        # Both calls should produce identical results
        assert len(outcome_lines_1) == len(outcome_lines_2) == 1
        assert outcome_lines_1[0] == outcome_lines_2[0]
        assert total_1 == total_2

        # Verify the content is correct (not doubled)
        outcome = outcome_lines_1[0]
        assert "Alice owes Bob:" in outcome
        assert "This draft: 30 tix" in outcome
        assert "Pre-existing: 0 tix" in outcome
        assert "Net total: 30 tix" in outcome

        # This would fail with the bug: "Pre-existing: 30 tix" and "Net total: 60 tix"
