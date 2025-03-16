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
    if use_optimized:
        stake_logger.info(f"Using optimized stake calculation algorithm with min_stake={min_stake}")
        return OptimizedStakeCalculator.calculate_stakes(team_a, team_b, stakes, min_stake, multiple)
    else:
        stake_logger.info(f"Using original stake calculation algorithm with min_stake={min_stake}")
        return StakeCalculator.calculate_stakes(team_a, team_b, stakes, min_stake)
    
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