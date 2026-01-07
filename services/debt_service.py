"""
Service for handling debt ledger operations.
"""
import asyncio
import uuid
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.exc import OperationalError
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from models.stake import StakeInfo


async def create_ledger_entries(
    guild_id: str,
    debtor_id: str,
    creditor_id: str,
    amount: int,
    source_type: str,
    source_id: str,
    notes: str = None,
    created_by: str = None
) -> tuple[DebtLedger, DebtLedger]:
    """
    Create a pair of ledger entries for a debt.

    Creates TWO entries (one from each player's perspective):
    - Entry 1: debtor's perspective (negative amount - they owe)
    - Entry 2: creditor's perspective (positive amount - they are owed)

    Args:
        guild_id: The guild this debt belongs to
        debtor_id: The player who owes money
        creditor_id: The player who is owed money
        amount: The amount owed (positive integer)
        source_type: 'draft', 'settlement', or 'admin'
        source_id: session_id for drafts, UUID for settlements/admin
        notes: Optional human-readable context
        created_by: Optional - who recorded this entry

    Returns:
        Tuple of (debtor_entry, creditor_entry)
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    logger.info(f"Creating ledger entries: {debtor_id} owes {creditor_id} {amount} tix (source: {source_type}/{source_id})")

    async with db_session() as session:
        # Entry from debtor's perspective (they owe, so negative)
        debtor_entry = DebtLedger(
            guild_id=guild_id,
            player_id=debtor_id,
            counterparty_id=creditor_id,
            amount=-amount,  # Negative: they owe
            source_type=source_type,
            source_id=source_id,
            notes=notes,
            created_by=created_by
        )

        # Entry from creditor's perspective (they are owed, so positive)
        creditor_entry = DebtLedger(
            guild_id=guild_id,
            player_id=creditor_id,
            counterparty_id=debtor_id,
            amount=amount,  # Positive: they are owed
            source_type=source_type,
            source_id=source_id,
            notes=notes,
            created_by=created_by
        )

        session.add(debtor_entry)
        session.add(creditor_entry)
        await session.commit()

        # Refresh to get the IDs
        await session.refresh(debtor_entry)
        await session.refresh(creditor_entry)

        logger.debug(f"Created ledger entries with IDs: {debtor_entry.id}, {creditor_entry.id}")

        return debtor_entry, creditor_entry


async def get_balance_with(
    guild_id: str,
    player_id: str,
    counterparty_id: str
) -> int:
    """
    Get the net balance between two players from player's perspective.

    Returns SUM(amount) for all entries where player is player_id and
    counterparty is counterparty_id. This gives:
    - Positive: counterparty owes player (player is owed)
    - Negative: player owes counterparty (player owes)
    - Zero: no debt between them

    Args:
        guild_id: The guild to check
        player_id: The player whose perspective we want
        counterparty_id: The other player

    Returns:
        Net balance as integer (positive = owed to player, negative = player owes)
    """
    async with db_session() as session:
        query = select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
            DebtLedger.guild_id == guild_id,
            DebtLedger.player_id == player_id,
            DebtLedger.counterparty_id == counterparty_id
        )
        result = await session.execute(query)
        balance = result.scalar()

        logger.debug(f"Balance for {player_id} with {counterparty_id} in {guild_id}: {balance}")

        return balance


async def get_all_balances_for(
    guild_id: str,
    player_id: str
) -> dict[str, int]:
    """
    Get all non-zero balances for a player with all their counterparties.

    Returns a dictionary mapping counterparty_id to net balance.
    Only includes counterparties with non-zero balances.

    Args:
        guild_id: The guild to check
        player_id: The player whose balances we want

    Returns:
        Dict of {counterparty_id: balance} for all non-zero balances
        Positive balance = counterparty owes player
        Negative balance = player owes counterparty
    """
    async with db_session() as session:
        # Group by counterparty and sum amounts
        query = (
            select(
                DebtLedger.counterparty_id,
                func.sum(DebtLedger.amount).label('balance')
            )
            .where(
                DebtLedger.guild_id == guild_id,
                DebtLedger.player_id == player_id
            )
            .group_by(DebtLedger.counterparty_id)
            .having(func.sum(DebtLedger.amount) != 0)
        )

        result = await session.execute(query)
        rows = result.all()

        balances = {row.counterparty_id: row.balance for row in rows}

        logger.debug(f"All balances for {player_id} in {guild_id}: {balances}")

        return balances


async def get_entries_since_last_settlement(
    guild_id: str,
    player_id: str,
    counterparty_id: str
) -> list[DebtLedger]:
    """
    Get all ledger entries between two players since their last settlement.

    Returns entries from player's perspective, in chronological order (oldest first).
    If no settlement exists, returns all entries.

    Args:
        guild_id: The guild to check
        player_id: The player whose perspective we want
        counterparty_id: The other player

    Returns:
        List of DebtLedger entries, oldest first
    """
    async with db_session() as session:
        # First, find the most recent settlement between these two players
        # (from player's perspective)
        settlement_query = (
            select(DebtLedger.created_at)
            .where(
                DebtLedger.guild_id == guild_id,
                DebtLedger.player_id == player_id,
                DebtLedger.counterparty_id == counterparty_id,
                DebtLedger.source_type == 'settlement'
            )
            .order_by(DebtLedger.created_at.desc())
            .limit(1)
        )

        settlement_result = await session.execute(settlement_query)
        last_settlement_time = settlement_result.scalar()

        # Build query for entries after last settlement (or all if no settlement)
        entries_query = (
            select(DebtLedger)
            .where(
                DebtLedger.guild_id == guild_id,
                DebtLedger.player_id == player_id,
                DebtLedger.counterparty_id == counterparty_id
            )
            .order_by(DebtLedger.created_at.asc())
        )

        if last_settlement_time is not None:
            entries_query = entries_query.where(
                DebtLedger.created_at > last_settlement_time
            )

        result = await session.execute(entries_query)
        entries = result.scalars().all()

        logger.debug(
            f"Found {len(entries)} entries for {player_id} with {counterparty_id} "
            f"since last settlement in {guild_id}"
        )

        return list(entries)


async def create_settlement(
    guild_id: str,
    payer_id: str,
    payee_id: str,
    amount: int,
    settled_by: str,
    settlement_id: str = None
) -> tuple[DebtLedger, DebtLedger]:
    """
    Create settlement entries to record a payment between two players.

    Settlement entries offset the balance:
    - Payer's entry: positive (reduces their debt)
    - Payee's entry: negative (reduces their credit)

    This function is idempotent when a settlement_id is provided - if entries
    with that source_id already exist, returns them instead of creating duplicates.

    Args:
        guild_id: The guild this settlement belongs to
        payer_id: The player who paid
        payee_id: The player who received payment
        amount: The amount paid (positive integer)
        settled_by: Who initiated the settlement (for audit)
        settlement_id: Optional deterministic ID for idempotency

    Returns:
        Tuple of (payer_entry, payee_entry)
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    # Use provided settlement_id or generate a new one
    if settlement_id is None:
        settlement_id = str(uuid.uuid4())

    notes = f"Settlement confirmed: {amount} tix"

    # Retry logic for transient database locks
    max_retries = 3
    retry_delay = 1.0  # seconds

    for attempt in range(max_retries):
        try:
            # Single session for idempotency check AND creation
            async with db_session() as session:
                # Idempotency check: see if this settlement already exists
                existing_query = (
                    select(DebtLedger)
                    .where(
                        DebtLedger.source_type == 'settlement',
                        DebtLedger.source_id == settlement_id
                    )
                    .order_by(DebtLedger.id)
                )
                result = await session.execute(existing_query)
                existing_entries = result.scalars().all()

                if len(existing_entries) >= 2:
                    logger.info(
                        f"Settlement {settlement_id} already exists, returning existing entries "
                        f"(idempotency check)"
                    )
                    return (existing_entries[0], existing_entries[1])

                # Create entries in the same session
                logger.info(
                    f"Creating settlement: {payer_id} paid {payee_id} {amount} tix "
                    f"(settled by {settled_by}, id: {settlement_id})"
                )

                # Entry from payer's perspective (they paid, so positive - reduces debt)
                payer_entry = DebtLedger(
                    guild_id=guild_id,
                    player_id=payer_id,
                    counterparty_id=payee_id,
                    amount=amount,  # Positive: reduces debt
                    source_type='settlement',
                    source_id=settlement_id,
                    notes=notes,
                    created_by=settled_by
                )

                # Entry from payee's perspective (they received, so negative - reduces credit)
                payee_entry = DebtLedger(
                    guild_id=guild_id,
                    player_id=payee_id,
                    counterparty_id=payer_id,
                    amount=-amount,  # Negative: reduces credit
                    source_type='settlement',
                    source_id=settlement_id,
                    notes=notes,
                    created_by=settled_by
                )

                session.add(payer_entry)
                session.add(payee_entry)
                # Commit happens automatically on session exit

                await session.flush()  # Ensure IDs are assigned
                await session.refresh(payer_entry)
                await session.refresh(payee_entry)

                logger.debug(f"Created settlement entries with IDs: {payer_entry.id}, {payee_entry.id}")

                return payer_entry, payee_entry

        except OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                logger.warning(
                    f"Database locked on settlement attempt {attempt + 1}/{max_retries}, "
                    f"retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                raise


async def create_debt_entries_from_stakes(
    guild_id: str,
    session_id: str,
    winning_team_ids: list[str]
) -> list[tuple[str, str, int]]:
    """
    Create debt ledger entries from stake outcomes when a draft completes.

    For each stake pair where players are on opposite teams, creates debt entries
    where the loser owes the winner.

    This function is idempotent - if debt entries already exist for this session,
    it will skip creation and return an empty list.

    Args:
        guild_id: The guild this draft belongs to
        session_id: The draft session ID
        winning_team_ids: List of player IDs on the winning team

    Returns:
        List of (loser_id, winner_id, amount) tuples for debts created
    """
    winning_team_set = set(winning_team_ids)
    debts_created = []

    # Single session for everything: idempotency check, read stakes, create all entries
    async with db_session() as session:
        # Idempotency check: see if debt entries already exist for this session
        existing_query = select(DebtLedger).where(
            DebtLedger.source_type == 'draft',
            DebtLedger.source_id == session_id
        ).limit(1)
        result = await session.execute(existing_query)
        if result.scalar():
            logger.debug(f"Debt entries already exist for session {session_id}, skipping")
            return []

        logger.info(f"Creating debt entries from stakes for session {session_id}")

        # Get all stake info records for this session
        stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
        results = await session.execute(stake_stmt)
        all_stake_infos = results.scalars().all()

        # Track processed pairs to avoid duplicates
        processed = set()

        for stake in all_stake_infos:
            # Skip if no opponent or amount
            if not stake.opponent_id or not stake.assigned_stake:
                continue

            # Create unique identifier for this pair (sorted players + amount)
            players = tuple(sorted([stake.player_id, stake.opponent_id]))
            amount = stake.assigned_stake
            pair_key = (players, amount)

            # Skip if already processed
            if pair_key in processed:
                continue
            processed.add(pair_key)

            # Check if players are on opposite teams
            player_a_on_winning = stake.player_id in winning_team_set
            player_b_on_winning = stake.opponent_id in winning_team_set

            if player_a_on_winning == player_b_on_winning:
                # Both winners or both losers - no debt
                continue

            # Determine winner (creditor) and loser (debtor)
            if player_a_on_winning:
                winner_id = stake.player_id
                loser_id = stake.opponent_id
            else:
                winner_id = stake.opponent_id
                loser_id = stake.player_id

            # Create debt ledger entries directly in this session (no separate function call)
            # Entry from debtor's perspective (they owe, so negative)
            debtor_entry = DebtLedger(
                guild_id=guild_id,
                player_id=loser_id,
                counterparty_id=winner_id,
                amount=-amount,  # Negative: they owe
                source_type='draft',
                source_id=session_id,
                notes=f"Draft #{session_id} stake outcome"
            )

            # Entry from creditor's perspective (they are owed, so positive)
            creditor_entry = DebtLedger(
                guild_id=guild_id,
                player_id=winner_id,
                counterparty_id=loser_id,
                amount=amount,  # Positive: they are owed
                source_type='draft',
                source_id=session_id,
                notes=f"Draft #{session_id} stake outcome"
            )

            session.add(debtor_entry)
            session.add(creditor_entry)

            debts_created.append((loser_id, winner_id, amount))

        # All entries committed in single transaction on session exit

    logger.info(f"Created {len(debts_created)} debt entries for session {session_id}")
    return debts_created
