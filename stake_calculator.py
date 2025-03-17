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
    
    @staticmethod
    def tiered_stakes_calculator(team_a: List[str], team_b: List[str], 
                                stakes: Dict[str, int], min_stake: int = 10,
                                multiple: int = 10) -> List[StakePair]:
        """
        Calculate stake pairings using a tiered approach that prioritizes 10/20/50 bets
        and applies proportional allocation to higher bets.
        """
        stake_logger.info(f"Starting tiered stake calculation with: Team A: {team_a}, Team B: {team_b}")
        stake_logger.info(f"Input stakes: {stakes}")
        stake_logger.info(f"Minimum stake: {min_stake}")
        
        # Handle outliers
        capped_stakes = handle_outliers(stakes)
        if capped_stakes != stakes:
            stake_logger.info("Outlier stakes detected and capped")
            stakes = capped_stakes
        
        # Create sorted lists of player stakes for each team
        team_a_stakes = [(player_id, stakes[player_id]) for player_id in team_a if player_id in stakes]
        team_b_stakes = [(player_id, stakes[player_id]) for player_id in team_b if player_id in stakes]
        
        # Calculate minimum required bet capacity for each team
        team_a_min_required = 0
        for player_id, stake in team_a_stakes:
            if stake <= 50:
                team_a_min_required += stake
            else:
                team_a_min_required += 50  # Can't reduce a 100+ bet below 50
        
        team_b_min_required = 0
        for player_id, stake in team_b_stakes:
            if stake <= 50:
                team_b_min_required += stake
            else:
                team_b_min_required += 50  # Can't reduce a 100+ bet below 50
        
        # Calculate total stakes for each team
        team_a_total = sum(stake for _, stake in team_a_stakes)
        team_b_total = sum(stake for _, stake in team_b_stakes)
        
        stake_logger.info(f"Team A total: {team_a_total}, minimum required: {team_a_min_required}")
        stake_logger.info(f"Team B total: {team_b_total}, minimum required: {team_b_min_required}")
        
        # Check if minimum requirements can be met
        if team_a_total < team_b_min_required or team_b_total < team_a_min_required:
            stake_logger.info(f"Minimum requirements not met, falling back to optimized algorithm")
            # Fall back to optimized algorithm
            return OptimizedStakeCalculator.calculate_stakes(team_a, team_b, stakes, min_stake, multiple)
        
        stake_logger.info(f"Minimum requirements met, proceeding with tiered algorithm")
        
        # Sort stakes by amount (highest first)
        team_a_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_stakes.sort(key=lambda x: x[1], reverse=True)
        
        stake_logger.info(f"Team A stakes after sorting: {team_a_stakes}")
        stake_logger.info(f"Team B stakes after sorting: {team_b_stakes}")
        
        # Identify low tiers (10/20/50) and high tiers (100+)
        team_a_low_tier = [(pid, stake) for pid, stake in team_a_stakes if stake <= 50]
        team_a_high_tier = [(pid, stake) for pid, stake in team_a_stakes if stake > 50]
        team_b_low_tier = [(pid, stake) for pid, stake in team_b_stakes if stake <= 50]
        team_b_high_tier = [(pid, stake) for pid, stake in team_b_stakes if stake > 50]
        
        stake_logger.info(f"Team A low tier (≤50): {team_a_low_tier}")
        stake_logger.info(f"Team A high tier (>50): {team_a_high_tier}")
        stake_logger.info(f"Team B low tier (≤50): {team_b_low_tier}")
        stake_logger.info(f"Team B high tier (>50): {team_b_high_tier}")
        
        # Calculate reserved amounts for low tiers
        team_a_reserved = sum(stake for _, stake in team_a_low_tier)
        team_b_reserved = sum(stake for _, stake in team_b_low_tier)
        
        # Calculate remaining capacity for high tiers
        team_a_remaining = team_a_total - team_a_reserved
        team_b_remaining = team_b_total - team_b_reserved
        
        stake_logger.info(f"Team A reserved for low tier: {team_a_reserved}, remaining: {team_a_remaining}")
        stake_logger.info(f"Team B reserved for low tier: {team_b_reserved}, remaining: {team_b_remaining}")
        
        # Initialize result pairs
        result_pairs = []
        
        # Critical: Track remaining stakes for each player to avoid exceeding max stakes
        remaining_stakes = {player_id: stake for player_id, stake in stakes.items()}
        
        # Process low tier stakes first (guaranteed 100% allocation)
        stake_logger.info(f"Processing low tier stakes (10/20/50)...")
        
        # Process team A low tier players
        for a_player_id, a_max_stake in team_a_low_tier:
            # Get current remaining stake
            a_remaining = remaining_stakes[a_player_id]
            stake_logger.info(f"Processing low tier player {a_player_id} with {a_remaining} tix remaining")
            
            # Skip if already fully allocated
            if a_remaining <= 0:
                continue
                
            # Try to match with team B's low tier players first
            for b_player_id, b_max_stake in team_b_low_tier:
                # Skip if this player is already fully allocated
                b_remaining = remaining_stakes[b_player_id]
                if b_remaining <= 0:
                    continue
                    
                # Calculate match amount - minimum of both players' remaining stakes
                match_amount = min(a_remaining, b_remaining)
                
                if match_amount >= min_stake:
                    # Create stake pair
                    stake_pair = StakePair(a_player_id, b_player_id, match_amount)
                    result_pairs.append(stake_pair)
                    
                    # Update remaining stakes for both players
                    remaining_stakes[a_player_id] -= match_amount
                    remaining_stakes[b_player_id] -= match_amount
                    
                    stake_logger.info(f"Low tier match: {a_player_id} ({a_max_stake}) with {b_player_id} ({b_max_stake}) for {match_amount} tix")
                    stake_logger.info(f"  {a_player_id} remaining: {remaining_stakes[a_player_id]}, {b_player_id} remaining: {remaining_stakes[b_player_id]}")
                    
                    # If player A is fully allocated, break the inner loop
                    a_remaining = remaining_stakes[a_player_id]
                    if a_remaining <= 0:
                        break
            
            # If player A still has remaining stake, try to match with high tier players
            a_remaining = remaining_stakes[a_player_id]
            if a_remaining > 0:
                for idx, (b_player_id, b_max_stake) in enumerate(team_b_high_tier):
                    b_remaining = remaining_stakes[b_player_id]
                    
                    # Skip if high tier player has no remaining stake
                    if b_remaining <= 0:
                        continue
                        
                    # Match amount is player A's remaining stake (high tier has plenty)
                    match_amount = min(a_remaining, b_remaining)
                    
                    if match_amount >= min_stake:
                        # Create stake pair
                        stake_pair = StakePair(a_player_id, b_player_id, match_amount)
                        result_pairs.append(stake_pair)
                        
                        # Update remaining stakes
                        remaining_stakes[a_player_id] -= match_amount
                        remaining_stakes[b_player_id] -= match_amount
                        
                        stake_logger.info(f"Low-High tier match: {a_player_id} ({a_max_stake}) with {b_player_id} ({b_max_stake}) for {match_amount} tix")
                        stake_logger.info(f"  {a_player_id} remaining: {remaining_stakes[a_player_id]}, {b_player_id} remaining: {remaining_stakes[b_player_id]}")
                        
                        # If player A is fully allocated, break
                        if remaining_stakes[a_player_id] <= 0:
                            break
        
        # Process team B low tier players
        for b_player_id, b_max_stake in team_b_low_tier:
            # Get current remaining stake
            b_remaining = remaining_stakes[b_player_id]
            stake_logger.info(f"Processing remaining low tier player {b_player_id} with {b_remaining} tix remaining")
            
            # Skip if already fully allocated
            if b_remaining <= 0:
                continue
                
            # Try to match with team A's high tier players
            for idx, (a_player_id, a_max_stake) in enumerate(team_a_high_tier):
                a_remaining = remaining_stakes[a_player_id]
                
                # Skip if high tier player has no remaining stake
                if a_remaining <= 0:
                    continue
                    
                # Match amount is player B's remaining stake (high tier has plenty)
                match_amount = min(b_remaining, a_remaining)
                
                if match_amount >= min_stake:
                    # Create stake pair
                    stake_pair = StakePair(a_player_id, b_player_id, match_amount)
                    result_pairs.append(stake_pair)
                    
                    # Update remaining stakes
                    remaining_stakes[a_player_id] -= match_amount
                    remaining_stakes[b_player_id] -= match_amount
                    
                    stake_logger.info(f"High-Low tier match: {a_player_id} ({a_max_stake}) with {b_player_id} ({b_max_stake}) for {match_amount} tix")
                    stake_logger.info(f"  {a_player_id} remaining: {remaining_stakes[a_player_id]}, {b_player_id} remaining: {remaining_stakes[b_player_id]}")
                    
                    # If player B is fully allocated, break
                    if remaining_stakes[b_player_id] <= 0:
                        break
        
        # Calculate remaining totals after low tier processing for high tier allocation
        team_a_high_remaining = sum(min(remaining_stakes[pid], stake) for pid, stake in team_a_high_tier)
        team_b_high_remaining = sum(min(remaining_stakes[pid], stake) for pid, stake in team_b_high_tier)
        
        stake_logger.info(f"After low tier processing: Team A high tier total: {team_a_high_remaining}")
        stake_logger.info(f"After low tier processing: Team B high tier total: {team_b_high_remaining}")
        
        # Apply proportional allocation to high tier bets
        if team_a_high_remaining > 0 and team_b_high_remaining > 0:
            stake_logger.info(f"Processing high tier stakes (>50)...")
            
            # Recalculate min/max high team after low tier processing
            if team_a_high_remaining <= team_b_high_remaining:
                min_high_team = [(pid, min(remaining_stakes[pid], stake)) for pid, stake in team_a_high_tier if remaining_stakes[pid] > 0]
                max_high_team = [(pid, min(remaining_stakes[pid], stake)) for pid, stake in team_b_high_tier if remaining_stakes[pid] > 0]
                min_high_team_name, max_high_team_name = "Team A", "Team B"
                min_high_team_total = team_a_high_remaining
                max_high_team_total = team_b_high_remaining
                is_team_a_min_high = True
            else:
                min_high_team = [(pid, min(remaining_stakes[pid], stake)) for pid, stake in team_b_high_tier if remaining_stakes[pid] > 0]
                max_high_team = [(pid, min(remaining_stakes[pid], stake)) for pid, stake in team_a_high_tier if remaining_stakes[pid] > 0]
                min_high_team_name, max_high_team_name = "Team B", "Team A"
                min_high_team_total = team_b_high_remaining
                max_high_team_total = team_a_high_remaining
                is_team_a_min_high = False
            
            stake_logger.info(f"Updated min high tier team: {min_high_team_name}, total: {min_high_team_total}")
            stake_logger.info(f"Updated max high tier team: {max_high_team_name}, total: {max_high_team_total}")
            
            # Calculate the equalized percentage for high tier bets
            if max_high_team_total > 0:
                equalized_percentage = (min_high_team_total / max_high_team_total) * 100
                equalized_percentage = min(equalized_percentage, 100.0)  # Cap at 100%
                
                stake_logger.info(f"Equalized percentage for high tier: {equalized_percentage:.2f}%")
                
                # Calculate adjusted allocations for max team
                max_high_allocations = []
                total_allocation = 0
                
                for player_id, stake in max_high_team:
                    # Calculate proportional allocation
                    allocation = stake * equalized_percentage / 100
                    
                    # Round to nearest multiple
                    rounded_allocation = round(allocation / multiple) * multiple
                    
                    # Ensure allocation is at least 50 tix for high tier bets
                    rounded_allocation = max(rounded_allocation, 50)
                    
                    # Ensure it doesn't exceed original stake
                    rounded_allocation = min(rounded_allocation, stake)
                    
                    # Add to list
                    max_high_allocations.append((player_id, rounded_allocation))
                    total_allocation += rounded_allocation
                    
                    stake_logger.info(f"High tier player {player_id}: {rounded_allocation}/{stake} = {(rounded_allocation/stake*100):.1f}%")
                
                # Check if adjustment is needed to match exactly
                adjustment_needed = min_high_team_total - total_allocation
                
                if abs(adjustment_needed) >= multiple:
                    stake_logger.info(f"Adjustment needed: {adjustment_needed} tix")
                    
                    if adjustment_needed > 0:
                        # Distribute additional capacity to players with the highest max stake first
                        sorted_allocations = []
                        for i, (player_id, current_allocation) in enumerate(max_high_allocations):
                            original_max = next(stake for pid, stake in max_high_team if pid == player_id)
                            sorted_allocations.append((i, player_id, current_allocation, original_max))
                        
                        # Sort by original max stake (highest first)
                        sorted_allocations.sort(key=lambda x: x[3], reverse=True)
                        
                        for idx, player_id, current_allocation, original_max in sorted_allocations:
                            room_left = original_max - current_allocation
                            
                            # Only adjust by multiples
                            adjustment = min(room_left, adjustment_needed)
                            adjustment = (adjustment // multiple) * multiple
                            
                            if adjustment > 0:
                                new_allocation = current_allocation + adjustment
                                max_high_allocations[idx] = (player_id, new_allocation)
                                adjustment_needed -= adjustment
                                stake_logger.info(f"Added {adjustment} to highest bettor {player_id}, now at {new_allocation}")
                                
                                if adjustment_needed < multiple:
                                    break
                    
                    elif adjustment_needed < 0:
                        # Remove excess capacity from the lowest non-min bettor first
                        sorted_allocations = []
                        for i, (player_id, current_allocation) in enumerate(max_high_allocations):
                            original_max = next(stake for pid, stake in max_high_team if pid == player_id)
                            # Only include players who aren't already at 50 tix
                            if current_allocation > 50:
                                sorted_allocations.append((i, player_id, current_allocation, original_max))
                        
                        # Sort by original max stake (lowest first)
                        sorted_allocations.sort(key=lambda x: x[3], reverse=False)
                        
                        for idx, player_id, current_allocation, original_max in sorted_allocations:
                            reducible_amount = current_allocation - 50  # Can't go below 50
                            
                            # Only adjust by multiples
                            adjustment = min(abs(adjustment_needed), reducible_amount)
                            adjustment = (adjustment // multiple) * multiple
                            
                            if adjustment > 0:
                                new_allocation = current_allocation - adjustment
                                max_high_allocations[idx] = (player_id, new_allocation)
                                adjustment_needed += adjustment
                                stake_logger.info(f"Removed {adjustment} from lowest bettor {player_id}, now at {new_allocation}")
                                
                                if adjustment_needed > -multiple:
                                    break
                
                # Match high tier players from min team with max team according to allocations
                for min_player_id, min_player_stake in min_high_team:
                    min_remaining = remaining_stakes[min_player_id]
                    if min_remaining <= 0:
                        continue
                        
                    # Match with max high tier players according to allocations
                    for max_idx, (max_player_id, max_allocation) in enumerate(max_high_allocations):
                        max_remaining = remaining_stakes[max_player_id]
                        if max_remaining <= 0:
                            continue
                            
                        # Determine how much to match
                        match_amount = min(min_remaining, max_allocation, max_remaining)
                        
                        if match_amount >= min_stake:  # Only create pairs above min stake
                            # Create the pair with correct team order
                            if is_team_a_min_high:
                                pair = StakePair(min_player_id, max_player_id, match_amount)
                            else:
                                pair = StakePair(max_player_id, min_player_id, match_amount)
                            
                            result_pairs.append(pair)
                            
                            # Update remaining stakes
                            remaining_stakes[min_player_id] -= match_amount
                            remaining_stakes[max_player_id] -= match_amount
                            
                            min_remaining = remaining_stakes[min_player_id]
                            max_remaining = remaining_stakes[max_player_id]
                            
                            stake_logger.info(f"Matched high tier: {min_player_id} ({min_player_stake}) with {max_player_id} ({max_allocation}) for {match_amount} tix")
                            stake_logger.info(f"  {min_player_id} remaining: {min_remaining}, {max_player_id} remaining: {max_remaining}")
                            
                            # If min player is fully allocated, break
                            if min_remaining <= 0:
                                break
        
        # Log the final allocation for each player
        stake_logger.info("Final allocation by player:")
        allocated_stakes = {player_id: stakes[player_id] - remaining for player_id, remaining in remaining_stakes.items()}
        
        for player_id, allocated in sorted(allocated_stakes.items(), key=lambda x: x[0]):
            if allocated > 0:
                max_stake = stakes[player_id]
                percentage = (allocated / max_stake) * 100
                stake_logger.info(f"Player {player_id}: {allocated}/{max_stake} tix ({percentage:.1f}%)")
        
        # Consolidate multiple bets between the same players
        stake_logger.info("Consolidating multiple bets between same players...")
        consolidated_pairs = []
        pair_map = {}
        
        for pair in result_pairs:
            # Create a unique key for each player pair (order matters)
            key = (pair.player_a_id, pair.player_b_id)
            
            if key in pair_map:
                # If we already have a pair with these players, add to the amount
                pair_map[key] += pair.amount
            else:
                # Otherwise, create a new entry
                pair_map[key] = pair.amount
        
        # Create consolidated pairs
        for key, amount in pair_map.items():
            player_a_id, player_b_id = key
            consolidated_pair = StakePair(player_a_id, player_b_id, amount)
            consolidated_pairs.append(consolidated_pair)
        
        stake_logger.info(f"Final stake pairs: {consolidated_pairs}")
        return consolidated_pairs

def calculate_stakes_with_strategy(team_a: List[str], team_b: List[str], 
                                  stakes: Dict[str, int], min_stake: int = 10,
                                  multiple: int = 10, use_optimized: bool = False) -> List[StakePair]:
    """
    Calculate stake pairings using either the original or optimized algorithm.
    
    Args:
        team_a: List of player IDs in team A
        team_b: List of player IDs in team B
        stakes: Dictionary mapping player IDs to their max stake
        min_stake: Minimum stake amount allowed (from user input in /dynamic_stake)
        multiple: Round stakes to this multiple (5 or 10)
        use_optimized: Whether to use the optimized algorithm
        
    Returns:
        List of StakePair objects representing the stake assignments
    """
    stake_logger.info(f"Using tiered stake calculation algorithm with min_stake={min_stake}")
    return StakeCalculator.tiered_stakes_calculator(team_a, team_b, stakes, min_stake, multiple)

    
class OptimizedStakeCalculator:
    @staticmethod
    def calculate_stakes(team_a: List[str], team_b: List[str], 
                         stakes: Dict[str, int], min_stake: int = 10,
                         multiple: int = 10) -> List[StakePair]:
        """
        Calculate stake pairings between two teams using the optimized bet score algorithm.
        
        This algorithm ensures:
        1. Min Team players get 100% of their bets allocated
        2. Max Team players have equalized bet scores (except min bettors)
        3. Min bet requirements are satisfied
        4. Transactions are minimized
        
        Args:
            team_a: List of player IDs in team A
            team_b: List of player IDs in team B
            stakes: Dictionary mapping player IDs to their max stake
            min_stake: Minimum stake amount allowed
            multiple: Round stakes to this multiple (5 or 10)
            
        Returns:
            List of StakePair objects representing the stake assignments
        """
        stake_logger.info(f"Starting optimized stake calculation with: Team A: {team_a}, Team B: {team_b}")
        stake_logger.info(f"Input stakes: {stakes}")
        stake_logger.info(f"Minimum stake: {min_stake}")
        
        # Create tuples of (player_id, stake) for each team
        team_a_stakes = [(player_id, stakes[player_id]) for player_id in team_a if player_id in stakes]
        team_b_stakes = [(player_id, stakes[player_id]) for player_id in team_b if player_id in stakes]
        
        # Sort by stake amount (descending)
        team_a_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_stakes.sort(key=lambda x: x[1], reverse=True)
        
        stake_logger.info(f"Team A stakes after sorting: {team_a_stakes}")
        stake_logger.info(f"Team B stakes after sorting: {team_b_stakes}")
        
        # Calculate team totals
        team_a_total = sum(stake for _, stake in team_a_stakes)
        team_b_total = sum(stake for _, stake in team_b_stakes)
        
        stake_logger.info(f"Team A total: {team_a_total}, Team B total: {team_b_total}")
        
        # Determine which is Min Team (lower total) and Max Team (higher total)
        if team_a_total <= team_b_total:
            min_team, max_team = team_a_stakes, team_b_stakes
            min_team_total, max_team_total = team_a_total, team_b_total
            is_team_a_min = True
        else:
            min_team, max_team = team_b_stakes, team_a_stakes
            min_team_total, max_team_total = team_b_total, team_a_total
            is_team_a_min = False
        
        stake_logger.info(f"Min Team: {min_team} (total: {min_team_total})")
        stake_logger.info(f"Max Team: {max_team} (total: {max_team_total})")
        
        # Step 1: Group players by those who are at min stake and those above
        min_stake_players = []
        above_min_players = []
        
        for player_id, max_stake in max_team:
            if max_stake <= min_stake:
                min_stake_players.append((player_id, max_stake))
            else:
                above_min_players.append((player_id, max_stake))
        
        # Step 2: For above_min_players, cap bets based on theoretical maximum allocation
        if above_min_players and min_team:
            # Calculate the theoretical max a single player could be allocated
            min_team_count = len(min_team)
            min_team_total_stakes = sum(stake for _, stake in min_team)
            
            # Formula: Min team total - ((min_team_count - 1) * min_stake)
            theoretical_max = min_team_total_stakes - ((min_team_count - 1) * min_stake)
            theoretical_max = max(theoretical_max, min_stake)  # Ensure at least min_stake
            
            stake_logger.info(f"Theoretical max bet: {theoretical_max} (min team total: {min_team_total_stakes}, players: {min_team_count})")
            
            # Iterate through above_min_players and cap any whose bet exceeds theoretical_max
            for i in range(len(above_min_players)):
                player_id, max_stake = above_min_players[i]
                if max_stake > theoretical_max:
                    above_min_players[i] = (player_id, theoretical_max)
                    stake_logger.info(f"Capped bettor {player_id} from {max_stake} to {theoretical_max}")
        
        # Step 3: Calculate total allocated to min stake players
        min_stake_allocation = sum(min(stake, min_stake) for _, stake in min_stake_players)
        
        # Step 4: Calculate remaining capacity for above-min players
        remaining_capacity = min_team_total - min_stake_allocation
        
        # Step 5: Calculate effective max for above-min players
        effective_max_total = sum(max_stake for _, max_stake in above_min_players)
        
        stake_logger.info(f"Min stake allocation: {min_stake_allocation}")
        stake_logger.info(f"Remaining capacity: {remaining_capacity}")
        stake_logger.info(f"Effective max total: {effective_max_total}")
        
        # Step 6: Calculate equalized bet score and allocations for Max Team
        all_allocations = []
        
        if above_min_players and effective_max_total > 0:
            # The equalized bet score is (remaining capacity) / (effective max total)
            bet_score = remaining_capacity / effective_max_total
            
            # Cap at 1.0 (100%)
            bet_score = min(bet_score, 1.0)
            
            stake_logger.info(f"Equalized bet score: {bet_score:.4f}")
            
            # Calculate allocations for above-min players
            above_min_allocations = []
            total_allocated = 0
            
            for player_id, max_stake in above_min_players:
                # Calculate allocation based on bet score
                allocation = max_stake * bet_score
                
                # Round to nearest multiple
                rounded_allocation = round(allocation / multiple) * multiple
                
                # Ensure minimum
                rounded_allocation = max(rounded_allocation, min_stake)
                
                above_min_allocations.append((player_id, rounded_allocation))
                total_allocated += rounded_allocation
                
                stake_logger.info(f"Player {player_id}: {rounded_allocation}/{max_stake} = {(rounded_allocation/max_stake)*100:.1f}%")
            
            # Adjust for rounding errors to match min team capacity exactly
            total_all_allocated = total_allocated + min_stake_allocation
            adjustment_needed = min_team_total - total_all_allocated
            
            if adjustment_needed != 0:
                stake_logger.info(f"Adjustment needed: {adjustment_needed}")
                
                # Apply adjustment to make totals match exactly
                if adjustment_needed > 0:
                    # Distribute additional capacity to players with the highest max stake first
                    # Sort players by their original max stake (highest first)
                    sorted_allocations = []
                    for i, (player_id, current_allocation) in enumerate(above_min_allocations):
                        original_max = next(stake for pid, stake in above_min_players if pid == player_id)
                        sorted_allocations.append((i, player_id, current_allocation, original_max))
                    
                    # Sort by original max stake (highest first)
                    sorted_allocations.sort(key=lambda x: x[3], reverse=True)
                    
                    for idx, player_id, current_allocation, original_max in sorted_allocations:
                        room_left = original_max - current_allocation
                        
                        # Only adjust by multiples
                        adjustment = min(room_left, adjustment_needed)
                        adjustment = (adjustment // multiple) * multiple
                        
                        if adjustment > 0:
                            new_allocation = current_allocation + adjustment
                            above_min_allocations[idx] = (player_id, new_allocation)
                            adjustment_needed -= adjustment
                            stake_logger.info(f"Added {adjustment} to highest bettor {player_id}, now at {new_allocation}")
                            
                            if adjustment_needed < multiple:
                                break
                
                elif adjustment_needed < 0:
                    # Remove excess capacity from the lowest non-min bettor first
                    # Sort players by their original max stake (lowest first)
                    sorted_allocations = []
                    for i, (player_id, current_allocation) in enumerate(above_min_allocations):
                        original_max = next(stake for pid, stake in above_min_players if pid == player_id)
                        # Only include players who aren't already at the minimum stake
                        if current_allocation > min_stake:
                            sorted_allocations.append((i, player_id, current_allocation, original_max))
                    
                    # Sort by original max stake (lowest first)
                    sorted_allocations.sort(key=lambda x: x[3], reverse=False)
                    
                    for i, player_id, current_allocation, original_max in sorted_allocations:
                        reducible_amount = current_allocation - min_stake
                        
                        # Only adjust by multiples
                        adjustment = min(abs(adjustment_needed), reducible_amount)
                        adjustment = (adjustment // multiple) * multiple
                        
                        if adjustment > 0:
                            new_allocation = current_allocation - adjustment
                            idx = next(idx for idx, (pid, _) in enumerate(above_min_allocations) if pid == player_id)
                            above_min_allocations[idx] = (player_id, new_allocation)
                            adjustment_needed += adjustment
                            stake_logger.info(f"Removed {adjustment} from lowest bettor {player_id}, now at {new_allocation}")
                            
                            if adjustment_needed > -multiple:
                                break
            
            # Combine allocations for all players
            all_allocations = above_min_allocations + [(pid, min(stake, min_stake)) for pid, stake in min_stake_players]
        else:
            # If all players are min stake, just allocate min stake to everyone
            all_allocations = [(pid, min(stake, min_stake)) for pid, stake in min_stake_players]
            
            stake_logger.info(f"All players at min stake: {all_allocations}")
        
        stake_logger.info(f"Final max team allocations: {all_allocations}")
        
        # Step 7: Create stake pairs ensuring Min Team players get 100% of their bets
        result_pairs = []
        
        # Create a dict of allocations for quick lookup
        max_team_allocations = {player_id: allocation for player_id, allocation in all_allocations}
        
        # Track allocated stakes
        min_player_allocated = {player_id: 0 for player_id, _ in min_team}
        max_player_allocated = {player_id: 0 for player_id, _ in all_allocations}
        
        # Process min team in order (highest stake first)
        for min_idx, (min_player_id, min_player_stake) in enumerate(min_team):
            remaining_min_stake = min_player_stake
            
            # Try to pair with max team players, starting with highest bettor
            for max_idx, (max_player_id, max_player_allocation) in enumerate(all_allocations):
                if remaining_min_stake == 0:
                    break  # This min player is fully allocated
                
                remaining_max_allocation = max_player_allocation - max_player_allocated.get(max_player_id, 0)
                
                if remaining_max_allocation >= min_stake:
                    # Determine how much to allocate for this pairing
                    pair_amount = min(remaining_min_stake, remaining_max_allocation)
                    
                    # Round down to nearest multiple, but only if not the final allocation
                    if pair_amount < remaining_min_stake or pair_amount % multiple == 0:
                        # Not the final allocation or already a multiple, round down
                        rounded_amount = (pair_amount // multiple) * multiple
                    else:
                        # This is the final allocation for this min player, don't round
                        rounded_amount = pair_amount
                    
                    # Ensure at least min stake
                    if rounded_amount >= min_stake:
                        # Create a stake pair with the right player order
                        if is_team_a_min:
                            pair = StakePair(min_player_id, max_player_id, rounded_amount)
                        else:
                            pair = StakePair(max_player_id, min_player_id, rounded_amount)
                            
                        result_pairs.append(pair)
                        min_player_allocated[min_player_id] += rounded_amount
                        max_player_allocated[max_player_id] += rounded_amount
                        
                        stake_logger.info(f"Pairing: Min player {min_player_id} with Max player {max_player_id} for {rounded_amount}")
                        
                        remaining_min_stake -= rounded_amount
        
        # Step 8: Add a post-processing step to ensure min team gets 100% allocation
        # Recalculate min_player_allocated from the pairs we've created so far
        min_player_allocated = {player_id: 0 for player_id, _ in min_team}
        for pair in result_pairs:
            min_player_id = pair.player_a_id if is_team_a_min else pair.player_b_id
            if min_player_id in min_player_allocated:
                min_player_allocated[min_player_id] += pair.amount

        for min_player_id, min_player_stake in min_team:
            allocated = min_player_allocated[min_player_id]
            if allocated < min_player_stake:
                remaining = min_player_stake - allocated
                stake_logger.info(f"Min player {min_player_id} needs {remaining} more tix for 100% allocation")
                
                # First try to add to an existing pair for this player (preferred approach)
                existing_pair_updated = False
                for i, pair in enumerate(result_pairs):
                    min_in_pair = pair.player_a_id if is_team_a_min else pair.player_b_id
                    max_in_pair = pair.player_b_id if is_team_a_min else pair.player_a_id
                    
                    if min_in_pair == min_player_id:
                        # We found an existing pair - update it directly with the full amount
                        if is_team_a_min:
                            updated_pair = StakePair(min_player_id, max_in_pair, pair.amount + remaining)
                        else:
                            updated_pair = StakePair(max_in_pair, min_player_id, pair.amount + remaining)
                        
                        # Replace the old pair with the updated one
                        result_pairs[i] = updated_pair
                        min_player_allocated[min_player_id] += remaining
                        stake_logger.info(f"Updated existing pair: added {remaining} to Min player {min_player_id}'s pair with {max_in_pair}")
                        existing_pair_updated = True
                        break
                
                # If we couldn't update any existing pair, create a new pair with any available max team player
                if not existing_pair_updated:
                    # Find any max team player with unallocated capacity
                    for max_player_id, max_stake in max_team:
                        max_allocated = 0
                        for pair in result_pairs:
                            if (is_team_a_min and pair.player_b_id == max_player_id) or \
                               (not is_team_a_min and pair.player_a_id == max_player_id):
                                max_allocated += pair.amount
                        
                        # If this max player has any capacity, use them
                        if max_allocated < max_stake:
                            max_remaining = max_stake - max_allocated
                            amount = min(remaining, max_remaining)
                            
                            # Create a new pair regardless of min_stake requirements
                            if is_team_a_min:
                                new_pair = StakePair(min_player_id, max_player_id, amount)
                            else:
                                new_pair = StakePair(max_player_id, min_player_id, amount)
                            
                            result_pairs.append(new_pair)
                            min_player_allocated[min_player_id] += amount
                            stake_logger.info(f"Created new pair: Min player {min_player_id} with Max player {max_player_id} for {amount} tix (force allocation)")
                            
                            remaining -= amount
                            if remaining <= 0:
                                break

                    # If we still have remaining stake to allocate, distribute it across existing pairs
                    if remaining > 0:
                        stake_logger.info(f"Still {remaining} unallocated for Min player {min_player_id} - distributing across existing pairs")
                        for i, pair in enumerate(result_pairs):
                            max_in_pair = pair.player_b_id if is_team_a_min else pair.player_a_id
                            
                            # Avoid modifying pairs involving our min player
                            min_in_pair = pair.player_a_id if is_team_a_min else pair.player_b_id
                            if min_in_pair == min_player_id:
                                continue
                            
                            # Create a new pair between our min player and this max player
                            if is_team_a_min:
                                new_pair = StakePair(min_player_id, max_in_pair, remaining)
                            else:
                                new_pair = StakePair(max_in_pair, min_player_id, remaining)
                            
                            result_pairs.append(new_pair)
                            min_player_allocated[min_player_id] += remaining
                            stake_logger.info(f"Emergency allocation: Min player {min_player_id} with Max player {max_in_pair} for {remaining} tix")
                            remaining = 0
                            break
        
        # Step 9: Try to consolidate multiple bets between the same players
        consolidated_pairs = []
        pair_map = {}
        
        for pair in result_pairs:
            # Create a unique key for each player pair (order matters based on original team assignment)
            if is_team_a_min:
                key = (pair.player_a_id, pair.player_b_id)
            else:
                key = (pair.player_b_id, pair.player_a_id)
                
            if key in pair_map:
                # If we already have a pair with these players, add to the amount
                pair_map[key] += pair.amount
            else:
                # Otherwise, create a new entry
                pair_map[key] = pair.amount
        
        # Create consolidated pairs
        for key, amount in pair_map.items():
            if is_team_a_min:
                min_player_id, max_player_id = key
            else:
                max_player_id, min_player_id = key
                
            if is_team_a_min:
                consolidated_pair = StakePair(min_player_id, max_player_id, amount)
            else:
                consolidated_pair = StakePair(max_player_id, min_player_id, amount)
                
            consolidated_pairs.append(consolidated_pair)
        
        # Log final stake satisfaction percentages
        stake_logger.info("Calculating final bet scores:")
        
        # Recalculate min_player_allocated from final pairs
        min_player_allocated = {player_id: 0 for player_id, _ in min_team}
        max_player_allocated = {player_id: 0 for player_id, _ in max_team}
        
        for pair in consolidated_pairs:
            if is_team_a_min:
                if pair.player_a_id in min_player_allocated:
                    min_player_allocated[pair.player_a_id] += pair.amount
                if pair.player_b_id in max_player_allocated:
                    max_player_allocated[pair.player_b_id] += pair.amount
            else:
                if pair.player_b_id in min_player_allocated:
                    min_player_allocated[pair.player_b_id] += pair.amount
                if pair.player_a_id in max_player_allocated:
                    max_player_allocated[pair.player_a_id] += pair.amount
        
        # Log Min Team satisfaction
        min_team_dict = dict(min_team)
        for player_id, max_bet in min_team:
            allocated = min_player_allocated.get(player_id, 0)
            if max_bet > 0:
                satisfaction = (allocated / max_bet) * 100
                stake_logger.info(f"Min Team Player {player_id}: {allocated}/{max_bet} = {satisfaction:.1f}%")
        
        # Log Max Team satisfaction
        max_team_dict = dict(max_team)
        for player_id, max_bet in max_team:
            allocated = max_player_allocated.get(player_id, 0)
            
            # For capped bettors, use their capped value
            if player_id in max_team_allocations:
                capped_max = max_team_allocations[player_id]
                denominator = capped_max
            else:
                denominator = max_bet
                
            if denominator > 0:
                satisfaction = (allocated / denominator) * 100
                stake_logger.info(f"Max Team Player {player_id}: {allocated}/{denominator} = {satisfaction:.1f}%")
        
        # Return consolidated pairs for a cleaner result
        return consolidated_pairs
    
def handle_outliers(stakes: Dict[str, int]):
    """Apply statistical outlier detection and capping"""

    values = list(stakes.values())

    # Calculate quartiles and IQR
    values.sort()
    n = len(values)
    q1_idx = n // 4
    q3_idx = (3 * n) // 4
    q1 = values[q1_idx]
    q3 = values[q3_idx]
    iqr = q3 - q1

    # Define upper bound for outliers, conservative approach of q3 + 1 * iqr (standard is 1.5 * iqr)
    upper_bound = q3 + iqr

    # Cap any outliers at the upper bound
    capped_stakes = {}
    outliers_found = False

    for player_id, stake in stakes.items():
        if stake > upper_bound:
            capped_stakes[player_id] = int(upper_bound)
            outliers_found = True
            stake_logger.info(f"Capped outlier bet: Player {player_id} from {stake} to {upper_bound}")
        else:
            capped_stakes[player_id] = stake
            
    if outliers_found:
        return capped_stakes
    else:
        return stakes