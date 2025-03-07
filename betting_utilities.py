import math
from datetime import datetime, timedelta
from sqlalchemy import select, update, and_
from loguru import logger
from database_utils import execute_with_retry
from session import AsyncSessionLocal, PlayerStats, DraftSession
from typing import List, Dict, Tuple, Optional

# Constants for odds calculation
MIN_ODDS = 1.1  # Minimum odds (very strong favorite)
MAX_ODDS = 15.0  # Maximum odds (extreme underdog)
BASE_TROPHY_ODDS = 5.0  # Base odds for a trophy (going 3-0)



async def get_trueskill_rating(user_id: str) -> Tuple[float, float]:
    """Get a player's TrueSkill rating from the database."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            query = select(PlayerStats).where(PlayerStats.player_id == user_id)
            result = await session.execute(query)
            player_stats = result.scalar_one_or_none()
            
            if player_stats:
                return player_stats.true_skill_mu, player_stats.true_skill_sigma
            else:
                # Default values for new players
                return 25.0, 8.333

async def calculate_team_win_probability(team_a_ids: List[str], team_b_ids: List[str]) -> Tuple[float, float, Optional[float]]:
    """Calculate win probabilities for team A and team B based on TrueSkill ratings."""
    # Get TrueSkill values for each player
    team_a_ratings = [await get_trueskill_rating(player_id) for player_id in team_a_ids]
    team_b_ratings = [await get_trueskill_rating(player_id) for player_id in team_b_ids]
    
    # Calculate team average ratings (mu and sigma)
    team_a_mu = sum(rating[0] for rating in team_a_ratings) / len(team_a_ratings)
    team_a_sigma = sum(rating[1] for rating in team_a_ratings) / len(team_a_ratings)
    
    team_b_mu = sum(rating[0] for rating in team_b_ratings) / len(team_b_ratings)
    team_b_sigma = sum(rating[1] for rating in team_b_ratings) / len(team_b_ratings)
    
    # Calculate win probability using TrueSkill formula
    # See: https://trueskill.org/
    denominator = math.sqrt(2 * (team_a_sigma**2 + team_b_sigma**2))
    if denominator == 0:
        # Avoid division by zero
        team_a_prob = 0.5
    else:
        diff = team_a_mu - team_b_mu
        team_a_prob = 0.5 * (1 + math.erf(diff / denominator))
    
    team_b_prob = 1 - team_a_prob
    
    # For 8-player drafts, calculate draw probability
    draw_prob = None
    if len(team_a_ids) == 4 and len(team_b_ids) == 4:
        # Simple approximation - higher chance of draw when teams are evenly matched
        # When teams are very close in skill, draw probability is higher
        skill_diff = abs(team_a_mu - team_b_mu)
        draw_prob = max(0, 0.15 - (skill_diff / 10) * 0.1)  # Up to 15% chance of draw
    
    return team_a_prob, team_b_prob, draw_prob

def convert_probability_to_odds(probability: float) -> float:
    """Convert a win probability to betting odds."""
    if probability <= 0:
        return MAX_ODDS
    if probability >= 1:
        return MIN_ODDS
    
    # Basic formula: odds = 1 / probability
    raw_odds = 1 / probability
    
    # Clamp to min/max range and apply a small margin
    margin = 1.1  
    adjusted_odds = raw_odds * margin
    
    # Round to 2 decimal places and clamp
    return max(MIN_ODDS, min(MAX_ODDS, round(adjusted_odds, 2)))

async def calculate_trophy_probability(player_id: str) -> float:
    """Calculate probability of a player getting a trophy (3-0)."""
    # Get player's TrueSkill rating
    mu, sigma = await get_trueskill_rating(player_id)
    
    # Adjust for skill - better players have better chances
    # This is a simplified approach - more complex models could be used
    base_trophy_chance = 0.125  # 1/8 chance for average player (in theory)
    
    # Scale based on skill (mu) compared to average (25)
    skill_factor = (mu - 15) / 20  # Normalized skill factor, 0 = below average, 1 = very good
    skill_factor = max(0, min(1, skill_factor))  # Clamp to [0, 1]
    
    # Calculate trophy probability - range from ~5% to ~25% based on skill
    trophy_probability = base_trophy_chance + (skill_factor * 0.125)
    
    return trophy_probability

async def create_betting_markets_for_draft(draft_session_id: str) -> List[int]:
    """Create betting markets for a draft session and return market IDs with retry logic."""
    from session import BettingMarket
    from session import AsyncSessionLocal, DraftSession
    from sqlalchemy import select
    
    async def _create_markets():
        market_ids = []
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get draft session
                draft_query = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                result = await session.execute(draft_query)
                draft = result.scalar_one_or_none()
                
                if not draft or not draft.team_a or not draft.team_b:
                    logger.error(f"Cannot create betting markets: draft {draft_session_id} not found or teams not set")
                    return []
                
                # Calculate team win probabilities
                team_a_prob, team_b_prob, draw_prob = await calculate_team_win_probability(draft.team_a, draft.team_b)
                
                # Convert to odds
                team_a_odds = convert_probability_to_odds(team_a_prob)
                team_b_odds = convert_probability_to_odds(team_b_prob)
                draw_odds = convert_probability_to_odds(draw_prob) if draw_prob is not None else None
                
                # Create team win market
                team_win_market = BettingMarket(
                    draft_session_id=draft_session_id,
                    market_type='team_win',
                    team_a_odds=team_a_odds,
                    team_b_odds=team_b_odds,
                    draw_odds=draw_odds
                )
                session.add(team_win_market)
                await session.flush()  # Flush to get the ID
                market_ids.append(team_win_market.id)
                
                # Create individual trophy markets for each player
                all_players = draft.team_a + draft.team_b
                
                for player_id in all_players:
                    trophy_prob = await calculate_trophy_probability(player_id)
                    trophy_odds = convert_probability_to_odds(trophy_prob)
                    
                    # Get player name from sign_ups
                    player_name = draft.sign_ups.get(player_id, f"Player {player_id}")
                    
                    trophy_market = BettingMarket(
                        draft_session_id=draft_session_id,
                        market_type='player_trophy',
                        player_id=player_id,
                        player_name=player_name,
                        trophy_odds=trophy_odds
                    )
                    session.add(trophy_market)
                    await session.flush()  # Flush to get the ID
                    market_ids.append(trophy_market.id)
                
                await session.commit()
                return market_ids
    
    try:
        # Execute with retry logic for database operations
        market_ids = await execute_with_retry(_create_markets)
        logger.info(f"Created {len(market_ids)} betting markets for draft {draft_session_id}")
        return market_ids
    except Exception as e:
        logger.error(f"Error creating betting markets: {e}")

async def get_or_create_user_wallet(user_id: str, display_name: str) -> Dict:
    """Get or create a user wallet and return wallet info."""
    from session import UserWallet
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            query = select(UserWallet).where(UserWallet.user_id == user_id)
            result = await session.execute(query)
            wallet = result.scalar_one_or_none()
            
            if not wallet:
                # Create new wallet with default balance
                wallet = UserWallet(
                    user_id=user_id,
                    display_name=display_name,
                    balance=1000,  # Starting balance
                    last_daily_claim=None
                )
                session.add(wallet)
                await session.flush()
                
            return {
                "user_id": wallet.user_id,
                "display_name": wallet.display_name,
                "balance": wallet.balance,
                "last_daily_claim": wallet.last_daily_claim
            }

async def place_bet(user_id: str, display_name: str, market_id: int, amount: int, outcome: str) -> Dict:
    """Place a bet on a market outcome and return bet info."""
    from session import UserWallet, BettingMarket, UserBet
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get user wallet
            wallet_query = select(UserWallet).where(UserWallet.user_id == user_id)
            wallet_result = await session.execute(wallet_query)
            wallet = wallet_result.scalar_one_or_none()
            
            if not wallet:
                wallet = UserWallet(
                    user_id=user_id,
                    display_name=display_name,
                    balance=1000
                )
                session.add(wallet)
                await session.flush()
            
            # Check if user has enough balance
            if wallet.balance < amount:
                return {"success": False, "error": "Insufficient balance"}
            
            # Get market
            market_query = select(BettingMarket).where(BettingMarket.id == market_id)
            market_result = await session.execute(market_query)
            market = market_result.scalar_one_or_none()
            
            if not market:
                return {"success": False, "error": "Betting market not found"}
            
            if market.status != 'open':
                return {"success": False, "error": "Betting market is closed"}
            
            # Get odds for selected outcome
            if market.market_type == 'team_win':
                if outcome == 'team_a':
                    odds = market.team_a_odds
                elif outcome == 'team_b':
                    odds = market.team_b_odds
                elif outcome == 'draw':
                    odds = market.draw_odds
                    if odds is None:
                        return {"success": False, "error": "Draw betting not available for this match"}
                else:
                    return {"success": False, "error": "Invalid outcome for team win market"}
            elif market.market_type == 'player_trophy':
                if outcome == 'trophy':
                    odds = market.trophy_odds
                elif outcome == 'no_trophy':
                    # No trophy odds are inverse of trophy odds, but with smaller payout
                    odds = 1 + ((1 / (market.trophy_odds - 1)) * 0.5)
                else:
                    return {"success": False, "error": "Invalid outcome for trophy market"}
            else:
                return {"success": False, "error": "Unknown market type"}
            
            # Calculate potential payout (bet amount * odds)
            potential_payout = int(amount * odds)
            
            # Create bet
            new_bet = UserBet(
                user_id=user_id,
                display_name=display_name,
                market_id=market_id,
                bet_amount=amount,
                selected_outcome=outcome,
                odds_at_bet_time=odds,
                potential_payout=potential_payout
            )
            session.add(new_bet)
            
            # Deduct amount from wallet
            wallet.balance -= amount
            
            await session.flush()
            
            return {
                "success": True,
                "bet_id": new_bet.id,
                "amount": amount,
                "odds": odds,
                "potential_payout": potential_payout,
                "new_balance": wallet.balance
            }

async def resolve_betting_markets(draft_session_id: str, team_a_wins: int, team_b_wins: int, trophy_winners: List[str] = None):
    """Resolve all betting markets for a completed draft."""
    from session import BettingMarket, UserBet, UserWallet
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get all markets for this draft
            markets_query = select(BettingMarket).where(
                and_(
                    BettingMarket.draft_session_id == draft_session_id,
                    BettingMarket.status == 'open'
                )
            )
            markets_result = await session.execute(markets_query)
            markets = markets_result.scalars().all()
            
            for market in markets:
                # Determine winning outcome
                if market.market_type == 'team_win':
                    if team_a_wins > team_b_wins:
                        winning_outcome = 'team_a'
                    elif team_b_wins > team_a_wins:
                        winning_outcome = 'team_b'
                    else:
                        winning_outcome = 'draw'
                elif market.market_type == 'player_trophy':
                    winning_outcome = 'trophy' if trophy_winners and market.player_id in trophy_winners else 'no_trophy'
                else:
                    continue  # Skip unknown market types
                
                # Update market status
                market.status = 'resolved'
                market.winning_outcome = winning_outcome
                
                # Get all bets for this market
                bets_query = select(UserBet).where(
                    and_(
                        UserBet.market_id == market.id,
                        UserBet.status == 'active'
                    )
                )
                bets_result = await session.execute(bets_query)
                bets = bets_result.scalars().all()
                
                # Process each bet
                for bet in bets:
                    if bet.selected_outcome == winning_outcome:
                        # Bet won - update status and pay out
                        bet.status = 'won'
                        
                        # Get user wallet and add winnings
                        wallet_query = select(UserWallet).where(UserWallet.user_id == bet.user_id)
                        wallet_result = await session.execute(wallet_query)
                        wallet = wallet_result.scalar_one_or_none()
                        
                        if wallet:
                            wallet.balance += bet.potential_payout
                    else:
                        # Bet lost
                        bet.status = 'lost'
                        
            await session.commit()

async def claim_daily_coins(user_id: str, display_name: str) -> Dict:
    """Let users claim daily coins to bet with."""
    from session import UserWallet
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get or create user wallet
            wallet_query = select(UserWallet).where(UserWallet.user_id == user_id)
            wallet_result = await session.execute(wallet_query)
            wallet = wallet_result.scalar_one_or_none()
            
            if not wallet:
                wallet = UserWallet(
                    user_id=user_id,
                    display_name=display_name,
                    balance=1000,
                    last_daily_claim=datetime.now()
                )
                session.add(wallet)
                await session.flush()
                return {
                    "success": True, 
                    "claimed": 1000, 
                    "balance": 1000,
                    "is_first_claim": True
                }
            
            # Check if user can claim daily coins
            now = datetime.now()
            if wallet.last_daily_claim:
                last_claim = wallet.last_daily_claim
                next_claim_time = last_claim + timedelta(days=1)
                
                if now < next_claim_time:
                    time_left = next_claim_time - now
                    hours, remainder = divmod(time_left.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    
                    return {
                        "success": False,
                        "error": f"Daily claim not ready yet. Try again in {hours}h {minutes}m"
                    }
            
            # Give daily coins
            daily_amount = 100
            wallet.balance += daily_amount
            wallet.last_daily_claim = now
            
            await session.commit()
            
            return {
                "success": True,
                "claimed": daily_amount,
                "balance": wallet.balance,
                "is_first_claim": False
            }

async def refund_all_bets(draft_session_id: str) -> dict:
    """Refund all active bets for a draft session that didn't complete."""
    from session import BettingMarket, UserBet, UserWallet
    from sqlalchemy import and_, select
    
    try:
        bet_count = 0
        total_amount = 0
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get all markets for this draft
                markets_query = select(BettingMarket).where(
                    BettingMarket.draft_session_id == draft_session_id
                )
                markets_result = await session.execute(markets_query)
                markets = markets_result.scalars().all()
                
                if not markets:
                    return {"success": True, "bet_count": 0, "total_amount": 0, "message": "No markets found"}
                
                market_ids = [market.id for market in markets]
                
                # Get all active bets for these markets
                bets_query = select(UserBet).where(
                    and_(
                        UserBet.market_id.in_(market_ids),
                        UserBet.status == 'active'
                    )
                )
                bets_result = await session.execute(bets_query)
                bets = bets_result.scalars().all()
                
                if not bets:
                    return {"success": True, "bet_count": 0, "total_amount": 0, "message": "No active bets found"}
                
                # Process refunds for each bet
                for bet in bets:
                    bet_count += 1
                    total_amount += bet.bet_amount
                    
                    # Update bet status
                    bet.status = 'refunded'
                    
                    # Refund the bet amount to the user's wallet
                    wallet_query = select(UserWallet).where(UserWallet.user_id == bet.user_id)
                    wallet_result = await session.execute(wallet_query)
                    wallet = wallet_result.scalar_one_or_none()
                    
                    if wallet:
                        wallet.balance += bet.bet_amount
                    else:
                        # If wallet doesn't exist (rare case), create one with the refunded amount
                        new_wallet = UserWallet(
                            user_id=bet.user_id,
                            display_name=bet.display_name,
                            balance=bet.bet_amount
                        )
                        session.add(new_wallet)
                
                # Update markets to cancelled status
                for market in markets:
                    market.status = 'cancelled'
                
                await session.commit()
                
                return {
                    "success": True,
                    "bet_count": bet_count,
                    "total_amount": total_amount,
                    "message": "All bets have been refunded"
                }
                
    except Exception as e:
        logger.error(f"Error refunding bets: {e}")
        return {
            "success": False,
            "bet_count": 0,
            "total_amount": 0,
            "message": f"Error: {str(e)}"
        }