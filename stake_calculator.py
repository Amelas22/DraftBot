# Update stake_calculator.py with enhanced logging

from typing import List, Dict
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Print to console
        logging.FileHandler('stake_calculator.log')  # Also log to a file
    ]
)

stake_logger = logging.getLogger('stake_calculator')
stake_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)

class StakePair:
    def __init__(self, player_a_id: str, player_b_id: str, amount: int):
        self.player_a_id = player_a_id
        self.player_b_id = player_b_id
        self.amount = amount
    
    def __repr__(self):
        return f"StakePair({self.player_a_id}, {self.player_b_id}, {self.amount})"


class StakeCalculator:
    @staticmethod
    def calculate_stakes(team_a: List[str], team_b: List[str], 
                         stakes: Dict[str, int], min_stake: int = 10) -> List[StakePair]:
        """
        Calculate stake pairings between two teams.
        
        Args:
            team_a: List of player IDs in team A
            team_b: List of player IDs in team B
            stakes: Dictionary mapping player IDs to their max stake
            min_stake: Minimum stake amount allowed
            
        Returns:
            List of StakePair objects representing the stake assignments
        """
        # Ensure team_a and team_b are lists, not None
        team_a = team_a or []
        team_b = team_b or []
        stakes = stakes or {}
        
        logger.info(f"Starting stake calculation with: Team A: {team_a}, Team B: {team_b}")
        logger.info(f"Input stakes: {stakes}")
        logger.info(f"Minimum stake: {min_stake}")
        
        # Filter out any players without stakes and ensure all stakes meet the minimum
        valid_stakes = {
            player_id: max(stake, min_stake) 
            for player_id, stake in stakes.items() 
            if player_id in team_a + team_b
        }
        
        logger.info(f"Valid stakes after filtering: {valid_stakes}")
        
        # Create sorted lists of (player_id, stake) tuples for each team
        team_a_stakes = [(p, valid_stakes.get(p, min_stake)) for p in team_a if p in valid_stakes]
        team_b_stakes = [(p, valid_stakes.get(p, min_stake)) for p in team_b if p in valid_stakes]
        
        logger.info(f"Team A stakes before sorting: {team_a_stakes}")
        logger.info(f"Team B stakes before sorting: {team_b_stakes}")
        
        # Sort by stake amount (descending)
        team_a_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_stakes.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"Team A stakes after sorting: {team_a_stakes}")
        logger.info(f"Team B stakes after sorting: {team_b_stakes}")
        
        # Create initial pairings based on stake order
        results = []
        remaining_a = []
        remaining_b = []
        
        # First pass: match players from both teams
        logger.info("Starting first pass of stake matching...")
        for idx in range(min(len(team_a_stakes), len(team_b_stakes))):
            player_a, stake_a = team_a_stakes[idx]
            player_b, stake_b = team_b_stakes[idx]
            
            # The bet amount is the minimum of the two stakes
            bet_amount = min(stake_a, stake_b)
            stake_pair = StakePair(player_a, player_b, bet_amount)
            results.append(stake_pair)
            
            logger.info(f"Match {idx+1}: {player_a} ({stake_a} tix) vs {player_b} ({stake_b} tix) = {bet_amount} tix")
            
            # Track remaining stake amounts for second pass
            if stake_a > bet_amount:
                remaining_a.append((player_a, stake_a - bet_amount))
                logger.info(f"  Player {player_a} has {stake_a - bet_amount} tix remaining")
            if stake_b > bet_amount:
                remaining_b.append((player_b, stake_b - bet_amount))
                logger.info(f"  Player {player_b} has {stake_b - bet_amount} tix remaining")
        
        # Handle any unmatched players from first pass
        if len(team_a_stakes) > len(team_b_stakes):
            for idx in range(len(team_b_stakes), len(team_a_stakes)):
                remaining_a.append(team_a_stakes[idx])
                logger.info(f"  Unmatched player {team_a_stakes[idx][0]} from Team A with {team_a_stakes[idx][1]} tix")
        elif len(team_b_stakes) > len(team_a_stakes):
            for idx in range(len(team_a_stakes), len(team_b_stakes)):
                remaining_b.append(team_b_stakes[idx])
                logger.info(f"  Unmatched player {team_b_stakes[idx][0]} from Team B with {team_b_stakes[idx][1]} tix")
        
        # Second pass: match players with remaining stakes
        logger.info("Starting second pass with remaining stakes...")
        logger.info(f"Remaining Team A stakes: {remaining_a}")
        logger.info(f"Remaining Team B stakes: {remaining_b}")
        
        remaining_a.sort(key=lambda x: x[1], reverse=True)
        remaining_b.sort(key=lambda x: x[1], reverse=True)
        
        while remaining_a and remaining_b:
            player_a, stake_a = remaining_a.pop(0)
            player_b, stake_b = remaining_b.pop(0)
            
            bet_amount = min(stake_a, stake_b)
            logger.info(f"Secondary match: {player_a} ({stake_a} tix) vs {player_b} ({stake_b} tix) = {bet_amount} tix")
            
            if bet_amount >= min_stake:
                stake_pair = StakePair(player_a, player_b, bet_amount)
                results.append(stake_pair)
                
                # Handle any leftover stakes
                if stake_a > bet_amount:
                    remaining_a.append((player_a, stake_a - bet_amount))
                    logger.info(f"  Player {player_a} still has {stake_a - bet_amount} tix remaining")
                if stake_b > bet_amount:
                    remaining_b.append((player_b, stake_b - bet_amount))
                    logger.info(f"  Player {player_b} still has {stake_b - bet_amount} tix remaining")
                
                # Resort the remaining lists
                remaining_a.sort(key=lambda x: x[1], reverse=True)
                remaining_b.sort(key=lambda x: x[1], reverse=True)
            else:
                logger.info(f"  Bet amount {bet_amount} is below minimum stake {min_stake}, skipping this pairing")
        
        logger.info(f"Final stake pairings: {results}")
        logger.info(f"Unused stakes from Team A: {remaining_a}")
        logger.info(f"Unused stakes from Team B: {remaining_b}")
        
        return results