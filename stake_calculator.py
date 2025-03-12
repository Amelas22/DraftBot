from typing import List, Dict
from loguru import logger
import sys
import os

stake_log_id = logger.add(
    "stake_calculator.log",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}",
    filter=lambda record: record["name"] == "stake_calculator",
    level="DEBUG",
    rotation="200 MB",
    enqueue=True  
)


stake_logger = logger.bind(name="stake_calculator")

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
        stake_logger.info(f"Starting stake calculation with: Team A: {team_a}, Team B: {team_b}")
        stake_logger.info(f"Input stakes: {stakes}")
        stake_logger.info(f"Minimum stake: {min_stake}")
        
        # Create sorted lists of (player_id, stake) tuples for each team
        team_a_stakes = [(p, stakes[p]) for p in team_a]
        team_b_stakes = [(p, stakes[p]) for p in team_b]
        
        # Sort by stake amount (descending)
        team_a_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_stakes.sort(key=lambda x: x[1], reverse=True)
        
        stake_logger.info(f"Team A stakes after sorting: {team_a_stakes}")
        stake_logger.info(f"Team B stakes after sorting: {team_b_stakes}")
        
        # Create initial pairings based on stake order
        results = []
        remaining_a = []
        remaining_b = []
        
        # First pass: match players from both teams
        stake_logger.info("Starting first pass of stake matching...")
        for idx in range(len(team_a_stakes)):
            player_a, stake_a = team_a_stakes[idx]
            player_b, stake_b = team_b_stakes[idx]
            
            # The bet amount is the minimum of the two stakes
            bet_amount = min(stake_a, stake_b)
            stake_pair = StakePair(player_a, player_b, bet_amount)
            results.append(stake_pair)
            
            stake_logger.info(f"Match {idx+1}: {player_a} ({stake_a} tix) vs {player_b} ({stake_b} tix) = {bet_amount} tix")
            
            # Track remaining stake amounts for second pass
            if stake_a > bet_amount:
                remaining_a.append((player_a, stake_a - bet_amount))
                stake_logger.debug(f"Player {player_a} has {stake_a - bet_amount} tix remaining")
            if stake_b > bet_amount:
                remaining_b.append((player_b, stake_b - bet_amount))
                stake_logger.debug(f"Player {player_b} has {stake_b - bet_amount} tix remaining")
        
        # Second pass: match players with remaining stakes
        if remaining_a and remaining_b:
            stake_logger.info("Starting second pass with remaining stakes...")
            stake_logger.info(f"Remaining Team A stakes: {remaining_a}")
            stake_logger.info(f"Remaining Team B stakes: {remaining_b}")
            
            # Sort remaining stakes by amount (highest first)
            remaining_a.sort(key=lambda x: x[1], reverse=True)
            remaining_b.sort(key=lambda x: x[1], reverse=True)
            
            # Ensure we're only processing active remainders
            final_remaining_a = []
            
            while remaining_a and remaining_b:
                player_a, stake_a = remaining_a.pop(0)
                player_b, stake_b = remaining_b.pop(0)
                
                bet_amount = min(stake_a, stake_b)
                stake_logger.info(f"Secondary match: {player_a} ({stake_a} tix) vs {player_b} ({stake_b} tix) = {bet_amount} tix")
                
                if bet_amount >= min_stake:
                    stake_pair = StakePair(player_a, player_b, bet_amount)
                    results.append(stake_pair)
                    
                    # Handle any leftover stakes
                    if stake_a > bet_amount:
                        new_stake_a = stake_a - bet_amount
                        final_remaining_a.append((player_a, new_stake_a))
                        stake_logger.debug(f"Player {player_a} still has {new_stake_a} tix remaining")
                    if stake_b > bet_amount:
                        remaining_b.append((player_b, stake_b - bet_amount))
                        remaining_b.sort(key=lambda x: x[1], reverse=True)
                        stake_logger.debug(f"Player {player_b} still has {stake_b - bet_amount} tix remaining")
                else:
                    stake_logger.info(f"Bet amount {bet_amount} is below minimum stake {min_stake}, skipping this pairing")
                    # Keep the stakes that were not used due to minimum stake
                    final_remaining_a.append((player_a, stake_a))
                    remaining_b.append((player_b, stake_b))
                    remaining_b.sort(key=lambda x: x[1], reverse=True)
            
            # Any leftovers from team A that weren't processed
            for player_a, stake_a in remaining_a:
                final_remaining_a.append((player_a, stake_a))
                
            # Any leftovers from final matches
            remaining_a = final_remaining_a
        
        # Log the final results
        stake_logger.info(f"Final stake pairings: {results}")
        if remaining_a:
            stake_logger.info(f"Unused stakes from Team A: {remaining_a}")
        if remaining_b:
            stake_logger.info(f"Unused stakes from Team B: {remaining_b}")
        
        return results