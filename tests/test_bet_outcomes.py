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

from datetime import datetime

from sqlalchemy import select

from conftest import seed_settlement
from utils import generate_draft_summary_embed, get_formatted_bet_outcomes
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.match import MatchResult
from models.stake import StakeInfo
from models.stake_pairing import StakePairing
from services.debt_service import create_ledger_entries


class _StubBot:
    """generate_draft_summary_embed only ever asks a bot for the guild, and the
    display-name lookups fall back cleanly when there isn't one."""

    def get_guild(self, guild_id):
        return None


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
                pairing = StakePairing(session_id="test_session_1", player_a_id="alice", player_b_id="bob", amount=30, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        # Call the function (Bob's team wins, so Alice loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_1",
            sign_ups,
            "B",  # winning_side
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
                pairing = StakePairing(session_id="test_session_2", player_a_id="alice", player_b_id="bob", amount=20, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        # Call the function (Bob wins again, Alice loses again)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_2",
            sign_ups,
            "B",
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
                pairing = StakePairing(session_id="test_session_3", player_a_id="alice", player_b_id="bob", amount=10, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_3",
            sign_ups,
            "A",  # Alice's side wins
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
                pairing = StakePairing(session_id="test_session_4", player_a_id="alice", player_b_id="bob", amount=20, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_4",
            sign_ups,
            "A",  # Alice's side wins
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
                pairing = StakePairing(session_id="test_session_5", player_a_id="alice", player_b_id="bob", amount=30, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        # Call the function (Alice wins, Bob loses 30)
        sign_ups = {"alice": "Alice", "bob": "Bob"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_5",
            sign_ups,
            "A",  # Alice's side wins
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

                pairing_ab = StakePairing(session_id="test_session_6", player_a_id="alice", player_b_id="bob", amount=30, side_a="A", side_b="B")
                pairing_cd = StakePairing(session_id="test_session_6", player_a_id="charlie", player_b_id="dave", amount=20, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, stake_charlie, stake_dave, pairing_ab, pairing_cd])
                await db_session.commit()

        # Call the function (Bob and Dave win)
        sign_ups = {"alice": "Alice", "bob": "Bob", "charlie": "Charlie", "dave": "Dave"}
        outcome_lines, total = await get_formatted_bet_outcomes(
            "test_session_6",
            sign_ups,
            "B",  # Team B wins
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
                pairing = StakePairing(session_id="test_session_idempotent", player_a_id="alice", player_b_id="bob",
                                        amount=30, side_a="A", side_b="B")
                db_session.add_all([stake_alice, stake_bob, pairing])
                await db_session.commit()

        sign_ups = {"alice": "Alice", "bob": "Bob"}
        winning_side = "B"

        # Call 1: Before debt entries are created
        outcome_lines_1, total_1 = await get_formatted_bet_outcomes(
            "test_session_idempotent",
            sign_ups,
            winning_side
        )

        # Create debt entries
        await create_debt_entries_from_stakes(
            guild_id="test_guild",
            session_id="test_session_idempotent",
            winning_side="B"
        )

        # Call 2: After debt entries are created
        outcome_lines_2, total_2 = await get_formatted_bet_outcomes(
            "test_session_idempotent",
            sign_ups,
            winning_side
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


class TestBetOutcomesAfterWalletSettlement:
    """A stake debt paid out of the loser's wallet must not read as a pre-existing debt.

    Auto-settlement writes its rows moments after the stake debt, in the same flow that
    renders this embed. The old pre-existing figure excluded only the draft rows for this
    session, so the settlement survived the filter and came back as a counter-debt: the
    embed announced "Pre-existing: 30 tix ... Net total: 0 tix (debts canceled!)" when
    nobody had owed anything beforehand and the loser had in fact just paid.
    """

    @pytest.mark.asyncio
    async def test_a_debt_paid_from_the_wallet_is_not_reported_as_pre_existing(self, test_db):
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                db_session.add(DraftSession(
                    session_id="settled_session", guild_id="test_guild",
                    team_a=["alice"], team_b=["bob"]))
                db_session.add(StakePairing(session_id="settled_session",
                                            player_a_id="alice", player_b_id="bob", amount=30,
                                            side_a="A", side_b="B"))
                await db_session.commit()

        # the draft's stake debt: alice owes bob 30
        await create_ledger_entries(
            guild_id="test_guild", debtor_id="alice", creditor_id="bob", amount=30,
            source_type="draft", source_id="settled_session")
        # auto-settlement pays it straight back out of alice's wallet. Written
        # directly (not via create_ledger_entries, which has no settlement_method
        # param) so this genuinely exercises the wallet path -- settlement_method
        # is what now distinguishes it from a manually-recorded settlement.
        await seed_settlement("test_guild", "alice", "bob", 30, "wallet", "some-uuid")

        outcome_lines, _ = await get_formatted_bet_outcomes(
            "settled_session", {"alice": "Alice", "bob": "Bob"}, "B")

        outcome = outcome_lines[0]
        assert "Pre-existing: 30" not in outcome, "nobody owed anything before this draft"
        assert "canceled" not in outcome.lower(), (
            "'canceled' means two debts offset each other; this one was PAID")
        assert "This draft: 30 tix" in outcome
        assert "aid" in outcome, "the line must say the money moved"
        assert "Nothing left to settle" in outcome

    @pytest.mark.asyncio
    async def test_a_partly_paid_debt_shows_what_is_still_owed(self, test_db):
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                db_session.add(DraftSession(
                    session_id="partial_session", guild_id="test_guild",
                    team_a=["alice"], team_b=["bob"]))
                db_session.add(StakePairing(session_id="partial_session",
                                            player_a_id="alice", player_b_id="bob", amount=100,
                                            side_a="A", side_b="B"))
                await db_session.commit()

        await create_ledger_entries(
            guild_id="test_guild", debtor_id="alice", creditor_id="bob", amount=100,
            source_type="draft", source_id="partial_session")
        # alice's wallet only covered 60 -- written directly (see the wallet
        # test above) so settlement_method='wallet' is set.
        await seed_settlement("test_guild", "alice", "bob", 60, "wallet", "some-uuid")

        outcome_lines, _ = await get_formatted_bet_outcomes(
            "partial_session", {"alice": "Alice", "bob": "Bob"}, "B")

        outcome = outcome_lines[0]
        assert "This draft: 100 tix" in outcome
        assert "60" in outcome, "the amount already paid must be visible"
        assert "40" in outcome, "and what is still owed"
        assert "Pre-existing: 60" not in outcome, "the payment is not a pre-existing debt"

    @pytest.mark.asyncio
    async def test_a_genuine_mutual_offset_still_reads_as_cancelled(self, test_db):
        """The word 'canceled' keeps its real meaning: two debts offsetting, no payment."""
        await create_ledger_entries(
            guild_id="test_guild", debtor_id="bob", creditor_id="alice", amount=30,
            source_type="draft", source_id="older_session")

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                db_session.add(DraftSession(
                    session_id="offset_session", guild_id="test_guild",
                    team_a=["alice"], team_b=["bob"]))
                db_session.add(StakePairing(session_id="offset_session",
                                            player_a_id="alice", player_b_id="bob", amount=30,
                                            side_a="A", side_b="B"))
                await db_session.commit()

        outcome_lines, _ = await get_formatted_bet_outcomes(
            "offset_session", {"alice": "Alice", "bob": "Bob"}, "B")

        outcome = outcome_lines[0]
        assert "Pre-existing" in outcome, "this one really is a pre-existing debt"
        assert "30" in outcome


class TestSettlementIsNotGatedOnTheDisplay:
    """Settlement must not depend on the bet-outcomes embed having lines.

    The embed is a rendering concern; booking the ledger rows is money. They used
    to be one branch: `create_debt_entries_from_stakes` sat inside
    `if outcome_lines:`, so anything that made the display produce nothing --
    a backer on neither roster, most obviously -- also silently skipped paying
    the winner.
    """

    async def _seed(self, session_id, pairing):
        """A finished staked draft that Team B won 2-0, plus one stake pairing."""
        async with AsyncSessionLocal() as db_session:
            db_session.add(DraftSession(
                session_id=session_id,
                guild_id="12345",
                session_type="staked",
                friendly_id="e2e-1",
                cube="TestCube",
                team_a=["alice"],
                team_b=["bob"],
                sign_ups={"alice": "Alice", "bob": "Bob",
                          "cyd": "Cyd", "dov": "Dov"},
                match_counter=3,
                draft_start_time=datetime(2026, 1, 1),
                teams_start_time=datetime(2026, 1, 1),
            ))
            db_session.add(MatchResult(
                session_id=session_id, match_number=1,
                player1_id="alice", player2_id="bob", winner_id="bob"))
            db_session.add(MatchResult(
                session_id=session_id, match_number=2,
                player1_id="alice", player2_id="bob", winner_id="bob"))
            db_session.add(pairing)
            await db_session.commit()

    async def _draft_debts(self, session_id):
        async with AsyncSessionLocal() as db_session:
            rows = (await db_session.execute(
                select(DebtLedger).where(DebtLedger.source_id == session_id)
            )).scalars().all()
        return {(r.player_id, r.counterparty_id, r.amount) for r in rows}

    @pytest.mark.asyncio
    async def test_a_wager_between_two_non_players_is_shown_and_settled(self, test_db):
        """The Phase 2 shape: both parties backed a side while sitting on neither
        roster. Roster inference reads both as "not on the winning team", drops the
        row from the display, and -- because settlement hung off the display -- pays
        the winner nothing."""
        await self._seed("spectator_summary", StakePairing(
            session_id="spectator_summary", player_a_id="cyd", player_b_id="dov",
            amount=25, side_a="A", side_b="B"))

        _, bet_embed = await generate_draft_summary_embed(_StubBot(), "spectator_summary")

        assert bet_embed is not None, "the wager is missing from the bet outcomes"
        # Team B won, dov backed B: cyd owes dov 25.
        assert await self._draft_debts("spectator_summary") == {
            ("cyd", "dov", -25), ("dov", "cyd", 25)}

