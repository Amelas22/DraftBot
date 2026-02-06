"""
Service for handling debt ledger operations.
"""
import asyncio
import uuid
from loguru import logger
from sqlalchemy import select, func, or_
from sqlalchemy.exc import OperationalError
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from models.stake import StakeInfo
from models.stake_pairing import StakePairing


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
    counterparty_id: str,
    exclude_session_id: str = None
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
        exclude_session_id: Optional session ID to exclude from balance calculation
                          (useful for getting pre-draft balance)

    Returns:
        Net balance as integer (positive = owed to player, negative = player owes)
    """
    async with db_session() as session:
        conditions = [
            DebtLedger.guild_id == guild_id,
            DebtLedger.player_id == player_id,
            DebtLedger.counterparty_id == counterparty_id
        ]

        # Exclude entries from a specific session if requested
        if exclude_session_id:
            conditions.append(
                or_(
                    DebtLedger.source_type != 'draft',
                    DebtLedger.source_id != exclude_session_id
                )
            )

        query = select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(*conditions)
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

                # Balance check: Verify payer owes money and isn't overpaying
                # We must run this query inside the same transaction to prevent race conditions
                balance_query = select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
                    DebtLedger.guild_id == guild_id,
                    DebtLedger.player_id == payer_id,
                    DebtLedger.counterparty_id == payee_id
                )
                balance_result = await session.execute(balance_query)
                current_balance = balance_result.scalar()

                # Payer needs to owe money (balance should be negative)
                if current_balance >= 0:
                    logger.warning(
                        f"Attempted settlement where payer {payer_id} owes nothing to {payee_id} "
                        f"(Balance: {current_balance}). Rejecting."
                    )
                    raise ValueError(f"You do not owe anything to this player (Balance: {current_balance})")

                # Check for overpayment
                debt_amount = abs(current_balance)
                if amount > debt_amount:
                     logger.warning(
                        f"Attempted overpayment: {payer_id} trying to pay {amount} to {payee_id} "
                        f"but only owes {debt_amount}. Rejecting."
                    )
                     raise ValueError(f"Payment amount ({amount}) exceeds current debt ({debt_amount})")

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

        # Get all stake pairings for this session
        pairing_stmt = select(StakePairing).where(StakePairing.session_id == session_id)
        results = await session.execute(pairing_stmt)
        all_pairings = results.scalars().all()

        # Track processed pairs to avoid duplicates (algorithm might create duplicates)
        processed = set()

        for pairing in all_pairings:
            # Create unique identifier for this pair (sorted players + amount)
            players = tuple(sorted([pairing.player_a_id, pairing.player_b_id]))
            pair_key = (players, pairing.amount)

            # Skip if already processed
            if pair_key in processed:
                continue
            processed.add(pair_key)

            # Check if players are on opposite teams
            player_a_on_winning = pairing.player_a_id in winning_team_set
            player_b_on_winning = pairing.player_b_id in winning_team_set

            if player_a_on_winning == player_b_on_winning:
                # Both winners or both losers - no debt
                continue

            # Determine winner (creditor) and loser (debtor)
            if player_a_on_winning:
                winner_id = pairing.player_a_id
                loser_id = pairing.player_b_id
            else:
                winner_id = pairing.player_b_id
                loser_id = pairing.player_a_id

            amount = pairing.amount

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


async def adjust_debt(
    guild_id: str,
    player1_id: str,
    player2_id: str,
    amount: int,
    notes: str,
    created_by: str
) -> int:
    """
    Adjust debt between two players (admin operation).

    Positive amount: player1 owes player2 more
    Negative amount: player1 owes player2 less

    This creates ledger entries with source_type='admin' using a double-entry
    system. Can be used to create new debt, adjust existing debt, or forgive debt.

    Args:
        guild_id: The guild this debt belongs to
        player1_id: First player
        player2_id: Second player
        amount: Amount to adjust (positive = player1 owes more, negative = player1 owes less)
        notes: Reason for this adjustment
        created_by: Admin who made this adjustment

    Returns:
        New balance from player1's perspective (negative = player1 owes player2)
    """
    if amount == 0:
        raise ValueError("Amount cannot be zero")

    admin_id = str(uuid.uuid4())

    logger.info(
        f"Admin {created_by} adjusting debt: {player1_id} <-> {player2_id} by {amount:+d} tix "
        f"(source_id: {admin_id})"
    )

    async with db_session() as session:
        # Create player1's entry
        # Positive amount means player1 owes more (negative entry in their ledger)
        # Negative amount means player1 owes less (positive entry in their ledger)
        player1_entry = DebtLedger(
            guild_id=guild_id,
            player_id=player1_id,
            counterparty_id=player2_id,
            amount=-amount,  # Invert: +50 means player1 owes 50 more (entry: -50)
            source_type='admin',
            source_id=admin_id,
            notes=notes,
            created_by=created_by
        )

        # Create player2's entry (opposite of player1)
        player2_entry = DebtLedger(
            guild_id=guild_id,
            player_id=player2_id,
            counterparty_id=player1_id,
            amount=amount,  # Opposite of player1's entry
            source_type='admin',
            source_id=admin_id,
            notes=notes,
            created_by=created_by
        )

        session.add(player1_entry)
        session.add(player2_entry)
        await session.commit()

        logger.debug(f"Created admin debt entries with IDs: {player1_entry.id}, {player2_entry.id}")

    # Return new balance from player1's perspective
    new_balance = await get_balance_with(guild_id, player1_id, player2_id)
    logger.info(f"New balance after adjustment: {player1_id} <-> {player2_id} = {new_balance}")
    return new_balance


async def get_guild_debt_stats(guild_id: str, timeframe: str = "all_time") -> dict:
    """
    Get comprehensive debt statistics for a guild.

    Args:
        guild_id: The guild to query
        timeframe: One of "all_time", "last_30_days", "last_7_days", "since_last_settlement"

    Returns:
        Dictionary containing:
        - total_debt: Sum of all negative balances
        - num_debtors: Count of unique players with negative balances
        - num_creditors: Count of unique players with positive balances
        - largest_debt: Tuple of (debtor_id, creditor_id, amount)
        - most_active_debtor: Tuple of (player_id, entry_count)
        - recent_activity: Number of new debt entries in timeframe
        - avg_debt_per_debtor: Average debt amount per debtor
        - debt_by_source: Dict of source_type to count
    """
    from datetime import datetime, timedelta

    # Calculate cutoff time based on timeframe
    cutoff_time = None
    if timeframe == "last_7_days":
        cutoff_time = datetime.utcnow() - timedelta(days=7)
    elif timeframe == "last_30_days":
        cutoff_time = datetime.utcnow() - timedelta(days=30)
    elif timeframe == "since_last_settlement":
        # Find most recent settlement in guild
        async with db_session() as session:
            query = (
                select(func.max(DebtLedger.created_at))
                .where(
                    DebtLedger.guild_id == guild_id,
                    DebtLedger.source_type == 'settlement'
                )
            )
            result = await session.execute(query)
            cutoff_time = result.scalar()

    async with db_session() as session:
        # Get all current balances (group by player/counterparty pairs)
        balance_query = (
            select(
                DebtLedger.player_id,
                DebtLedger.counterparty_id,
                func.sum(DebtLedger.amount).label('balance')
            )
            .where(DebtLedger.guild_id == guild_id)
            .group_by(DebtLedger.player_id, DebtLedger.counterparty_id)
            .having(func.sum(DebtLedger.amount) != 0)
        )
        balance_result = await session.execute(balance_query)
        balances = balance_result.all()

        # Calculate totals
        total_debt = 0
        debtors = set()
        creditors = set()
        largest_debt = None
        largest_debt_amount = 0

        for row in balances:
            if row.balance < 0:
                # This player owes money
                total_debt += abs(row.balance)
                debtors.add(row.player_id)
                if abs(row.balance) > largest_debt_amount:
                    largest_debt_amount = abs(row.balance)
                    largest_debt = (row.player_id, row.counterparty_id, abs(row.balance))
            else:
                # This player is owed money
                creditors.add(row.player_id)

        # Get activity stats (filtered by timeframe)
        activity_query = select(
            DebtLedger.player_id,
            func.count(DebtLedger.id).label('entry_count')
        ).where(DebtLedger.guild_id == guild_id)

        if cutoff_time:
            activity_query = activity_query.where(DebtLedger.created_at >= cutoff_time)

        activity_query = activity_query.group_by(DebtLedger.player_id).order_by(
            func.count(DebtLedger.id).desc()
        )

        activity_result = await session.execute(activity_query)
        activity_rows = activity_result.all()

        most_active_debtor = activity_rows[0] if activity_rows else None

        # Get total entry count for recent activity
        recent_count_query = select(func.count(DebtLedger.id)).where(
            DebtLedger.guild_id == guild_id
        )
        if cutoff_time:
            recent_count_query = recent_count_query.where(DebtLedger.created_at >= cutoff_time)

        recent_count_result = await session.execute(recent_count_query)
        recent_activity = recent_count_result.scalar()

        # Get debt breakdown by source type (within timeframe)
        source_query = select(
            DebtLedger.source_type,
            func.count(DebtLedger.id).label('count')
        ).where(DebtLedger.guild_id == guild_id)

        if cutoff_time:
            source_query = source_query.where(DebtLedger.created_at >= cutoff_time)

        source_query = source_query.group_by(DebtLedger.source_type)

        source_result = await session.execute(source_query)
        source_rows = source_result.all()
        debt_by_source = {row.source_type: row.count for row in source_rows}

    # Calculate average debt per debtor
    avg_debt_per_debtor = total_debt / len(debtors) if debtors else 0

    stats = {
        'total_debt': total_debt,
        'num_debtors': len(debtors),
        'num_creditors': len(creditors),
        'largest_debt': largest_debt,
        'most_active_debtor': most_active_debtor,
        'recent_activity': recent_activity,
        'avg_debt_per_debtor': avg_debt_per_debtor,
        'debt_by_source': debt_by_source,
        'timeframe': timeframe
    }

    logger.debug(f"Guild stats for {guild_id} ({timeframe}): {stats}")
    return stats


async def get_debt_history(
    guild_id: str,
    player_id: str = None,
    limit: int = 25
) -> list[DebtLedger]:
    """
    Get history of all debt entries.

    Returns all ledger entries (draft, settlement, and admin), optionally filtered
    by a specific player. Entries are returned in reverse chronological order
    (newest first).

    Args:
        guild_id: The guild to query
        player_id: Optional - filter to entries involving this player
        limit: Maximum number of entries to return (default 25, max 100)

    Returns:
        List of DebtLedger entries
    """
    limit = min(limit, 100)  # Cap at 100

    async with db_session() as session:
        query = (
            select(DebtLedger)
            .where(DebtLedger.guild_id == guild_id)
        )

        if player_id:
            # Filter to entries where player is either player_id or counterparty_id
            from sqlalchemy import or_
            query = query.where(
                or_(
                    DebtLedger.player_id == player_id,
                    DebtLedger.counterparty_id == player_id
                )
            )

        query = query.order_by(DebtLedger.created_at.desc()).limit(limit)

        result = await session.execute(query)
        entries = result.scalars().all()

        logger.debug(
            f"Found {len(entries)} debt history entries for guild {guild_id}"
            + (f" (filtered to player {player_id})" if player_id else "")
        )

        return list(entries)


async def get_guild_debt_rows(guild_id: str) -> list:
    """
    Get all debt relationships for a guild (from debtor perspective).

    Returns rows with player_id (debtor), counterparty_id (creditor), and balance.
    Only returns negative balances (actual debts), ordered by largest debt first.

    Args:
        guild_id: The guild to query

    Returns:
        List of Row objects with player_id, counterparty_id, balance attributes
    """
    async with db_session() as session:
        query = (
            select(
                DebtLedger.player_id,
                DebtLedger.counterparty_id,
                func.sum(DebtLedger.amount).label('balance')
            )
            .where(DebtLedger.guild_id == guild_id)
            .group_by(DebtLedger.player_id, DebtLedger.counterparty_id)
            .having(func.sum(DebtLedger.amount) < 0)
            .order_by(func.sum(DebtLedger.amount).asc())
        )
        result = await session.execute(query)
        return result.all()
