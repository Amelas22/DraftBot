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
    
    def tiered_stakes_calculator(team_a: List[str], team_b: List[str], 
                                    stakes: Dict[str, int], min_stake: int = 10,
                                    multiple: int = 10) -> List[StakePair]:
        """
        Calculate stake pairings using a tiered approach that prioritizes 10/20/50 bets
        and applies proportional allocation to higher bets.
        
        This uses a two-phase approach:
        1. First determine optimal individual allocations for each player
        2. Then create efficient pairings to minimize transactions
        """
        try:
            # Create a deep copy of stakes to avoid modifying the original
            import copy
            stakes = copy.deepcopy(stakes)
            
            # Track original stakes before any MTMB adjustments
            original_stakes = copy.deepcopy(stakes)
            
            stake_logger.info(f"Starting tiered stake calculation with: Team A: {team_a}, Team B: {team_b}")
            stake_logger.info(f"Input stakes: {stakes}")
            stake_logger.info(f"Minimum stake: {min_stake}")
            
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
            
            # ------------------------------------------------------
            # MTMB (Modified Theoretical Max Bid) calculation
            # ------------------------------------------------------
            
            # Determine min and max teams for MTMB calculation
            if team_a_total <= team_b_total:
                min_team = team_a_stakes.copy()
                max_team = team_b_stakes.copy()
                min_team_id = 'A'
                max_team_id = 'B'
                min_team_total = team_a_total
                max_team_total = team_b_total
            else:
                min_team = team_b_stakes.copy()
                max_team = team_a_stakes.copy()
                min_team_id = 'B'
                max_team_id = 'A'
                min_team_total = team_b_total
                max_team_total = team_a_total
            
            stake_logger.info(f"MTMB calculation - Min team: {min_team_id}, total: {min_team_total}")
            stake_logger.info(f"MTMB calculation - Max team: {max_team_id}, total: {max_team_total}")
            
            # Calculate MTMB (Modified Theoretical Max Bid)
            # Identify low tiers (≤50) and high tiers (>50) in max team
            max_team_low_tier = [(pid, stake) for pid, stake in max_team if stake <= 50]
            max_team_high_tier = [(pid, stake) for pid, stake in max_team if stake > 50]
            
            # Sort high tier by stake (descending)
            max_team_high_tier.sort(key=lambda x: x[1], reverse=True)
            
            reserved_amount = 0
            
            # Add all low tier bets (≤50)
            for _, stake in max_team_low_tier:
                reserved_amount += stake
            
            # Add all high tier bets EXCEPT the highest one, reduced to 50
            if len(max_team_high_tier) > 1:
                for _, stake in max_team_high_tier[1:]:  # Skip the highest bettor
                    reserved_amount += 50  # High tier bets reduce to 50 minimum
            
            mtmb = min_team_total - reserved_amount
            mtmb = max(mtmb, 50)  # Ensure MTMB is at least 50
            
            stake_logger.info(f"MTMB calculation - Max team low tier: {max_team_low_tier}")
            stake_logger.info(f"MTMB calculation - Max team high tier: {max_team_high_tier}")
            stake_logger.info(f"MTMB calculation - Reserved amount: {reserved_amount}")
            stake_logger.info(f"MTMB calculation - Result: {mtmb}")
            
            # Apply MTMB to cap outlier bets in max team
            mtmb_applied = False
            for i, (player_id, stake) in enumerate(max_team):
                if stake > mtmb:
                    mtmb_applied = True
                    stakes[player_id] = mtmb  # Update the stakes dictionary
                    stake_logger.info(f"Capped Team {max_team_id} bettor {player_id} from {stake} to {mtmb}")
            
            if mtmb_applied:
                # Rebuild team arrays with the updated stakes
                if min_team_id == 'A':
                    team_a_stakes = [(player_id, stakes[player_id]) for player_id in team_a if player_id in stakes]
                    team_b_stakes = [(player_id, stakes[player_id]) for player_id in team_b if player_id in stakes]
                else:
                    team_b_stakes = [(player_id, stakes[player_id]) for player_id in team_b if player_id in stakes]
                    team_a_stakes = [(player_id, stakes[player_id]) for player_id in team_a if player_id in stakes]
                    
                # Recalculate team totals
                team_a_total = sum(stake for _, stake in team_a_stakes)
                team_b_total = sum(stake for _, stake in team_b_stakes)
                
                # Recalculate min and max teams
                if team_a_total <= team_b_total:
                    min_team = team_a_stakes.copy()
                    max_team = team_b_stakes.copy()
                    min_team_id = 'A'
                    max_team_id = 'B'
                    min_team_total = team_a_total
                    max_team_total = team_b_total
                else:
                    min_team = team_b_stakes.copy()
                    max_team = team_a_stakes.copy()
                    min_team_id = 'B'
                    max_team_id = 'A'
                    min_team_total = team_b_total
                    max_team_total = team_a_total
                    
                stake_logger.info(f"After MTMB adjustment - Team A total: {team_a_total}, Team B total: {team_b_total}")
                stake_logger.info(f"After MTMB adjustment - Min team: {min_team_id}, total: {min_team_total}")
                stake_logger.info(f"After MTMB adjustment - Max team: {max_team_id}, total: {max_team_total}")
            
            # ------------------------------------------------------
            # Phase 1: Calculate Individual Allocations
            # ------------------------------------------------------
            stake_logger.info("Phase 1: Calculating individual allocations")
            
            # Identify low tiers (≤50) and high tiers (>50) in both teams
            min_team_low_tier = [(pid, stake) for pid, stake in min_team if stake <= 50]
            min_team_high_tier = [(pid, stake) for pid, stake in min_team if stake > 50]
            max_team_low_tier = [(pid, stake) for pid, stake in max_team if stake <= 50]
            max_team_high_tier = [(pid, stake) for pid, stake in max_team if stake > 50]
            
            stake_logger.info(f"Min team low tier (≤50): {min_team_low_tier}")
            stake_logger.info(f"Min team high tier (>50): {min_team_high_tier}")
            stake_logger.info(f"Max team low tier (≤50): {max_team_low_tier}")
            stake_logger.info(f"Max team high tier (>50): {max_team_high_tier}")
            
            # Calculate initial allocations
            # 1. All min team players get 100% of their bets
            # 2. All max team low tier players (≤50) get 100% of their bets
            
            # Initialize allocations dictionary
            allocations = {}
            
            # Min team gets 100% allocation
            for player_id, stake in min_team:
                allocations[player_id] = stake
                stake_logger.info(f"Min team player {player_id} allocation: {stake}/{stake} (100%)")
            
            # Max team low tier gets 100% allocation
            max_team_low_tier_total = 0
            for player_id, stake in max_team_low_tier:
                allocations[player_id] = stake
                max_team_low_tier_total += stake
                stake_logger.info(f"Max team low tier player {player_id} allocation: {stake}/{stake} (100%)")
            
            # Calculate remaining capacity for high tier bets
            remaining_capacity = min_team_total - max_team_low_tier_total
            stake_logger.info(f"Remaining capacity for high tier: {remaining_capacity}")
            
            # Calculate total high tier bets on max team
            max_team_high_tier_total = sum(stake for _, stake in max_team_high_tier)
            
            # Distribute remaining capacity proportionally to high tier bettors
            if max_team_high_tier and max_team_high_tier_total > 0:
                # Track original max stakes and sort by them (highest first)
                original_high_tier = [(pid, stakes[pid], original_stakes[pid]) for pid, _ in max_team_high_tier]
                original_high_tier.sort(key=lambda x: x[2], reverse=True)
                
                # Calculate allocation percentage
                allocation_percentage = min(100, (remaining_capacity / max_team_high_tier_total) * 100)
                stake_logger.info(f"High tier allocation percentage: {allocation_percentage:.2f}%")
                
                # Allocate to each high tier bettor
                total_high_tier_allocated = 0
                high_tier_allocations = []
                
                for player_id, adjusted_stake, original_stake in original_high_tier:
                    # Calculate raw allocation
                    raw_allocation = adjusted_stake * allocation_percentage / 100
                    
                    # Round to nearest multiple
                    rounded_allocation = (raw_allocation // multiple) * multiple
                    
                    # Ensure at least minimum stake
                    rounded_allocation = max(rounded_allocation, min_stake)
                    
                    # But don't exceed the adjusted stake
                    rounded_allocation = min(rounded_allocation, adjusted_stake)
                    
                    high_tier_allocations.append((player_id, rounded_allocation, adjusted_stake, original_stake))
                    total_high_tier_allocated += rounded_allocation
                
                # Check if adjustment is needed
                adjustment_needed = remaining_capacity - total_high_tier_allocated
                stake_logger.info(f"High tier allocation adjustment needed: {adjustment_needed}")
                
                # Adjust allocations if needed
                if adjustment_needed > 0 and adjustment_needed >= multiple:
                    # Sort high tier bettors by original stake (highest first)
                    high_tier_allocations.sort(key=lambda x: x[3], reverse=True)
                    
                    # Distribute adjustment evenly among top bettors
                    # First, identify eligible bettors (those with room for adjustment)
                    eligible_bettors = []
                    for i, (player_id, current_allocation, adjusted_stake, _) in enumerate(high_tier_allocations):
                        room_left = adjusted_stake - current_allocation
                        if room_left >= multiple:
                            eligible_bettors.append((i, player_id, current_allocation, adjusted_stake, room_left))
                    
                    if eligible_bettors:
                        # Calculate how many bettors to distribute to
                        num_bettors = min(len(eligible_bettors), int(adjustment_needed // multiple))

                        if eligible_bettors and num_bettors > 0:
                            # Use float division to calculate proper fair share
                            adjustment_per_bettor = adjustment_needed / num_bettors
                            # Round to nearest multiple
                            fair_share = int(adjustment_per_bettor / multiple) * multiple
                            # Ensure at least one multiple per bettor if there's enough adjustment needed
                            if fair_share < multiple and adjustment_needed >= multiple * num_bettors:
                                fair_share = multiple
                            
                            remaining_adjustment = adjustment_needed
                            
                            # Distribute evenly with tracking of remaining adjustment
                            for j in range(num_bettors):
                                i, player_id, current_allocation, adjusted_stake, room_left = eligible_bettors[j]
                                
                                # Calculate this bettor's share (not exceeding room left)
                                this_share = min(fair_share, room_left)
                                this_share = min(this_share, remaining_adjustment)  # Don't exceed remaining adjustment
                                
                                if this_share >= multiple:
                                    # Update allocation
                                    new_allocation = current_allocation + this_share
                                    high_tier_allocations[i] = (player_id, new_allocation, adjusted_stake, high_tier_allocations[i][3])
                                    remaining_adjustment -= this_share
                                    stake_logger.info(f"Added {this_share} to high bettor {player_id}, now at {new_allocation}")
                            
                            # If there's still adjustment needed, distribute the remainder more fairly
                            if remaining_adjustment >= multiple:
                                # Sort eligible bettors by room left (most room first) and then by original stake (highest first)
                                remaining_eligible = []
                                for i, player_id, current_allocation, adjusted_stake, room_left in eligible_bettors:
                                    # Get updated allocation
                                    updated_current = high_tier_allocations[i][1]
                                    updated_room_left = adjusted_stake - updated_current
                                    original_stake = high_tier_allocations[i][3]
                                    
                                    if updated_room_left >= multiple:
                                        remaining_eligible.append((i, player_id, updated_current, adjusted_stake, updated_room_left, original_stake))
                                
                                # Sort by room left (descending) and then by original stake (descending)
                                remaining_eligible.sort(key=lambda x: (x[4], x[5]), reverse=True)
                                
                                # Distribute remaining adjustment fairly across eligible bettors
                                while remaining_adjustment >= multiple and remaining_eligible:
                                    for idx, (i, player_id, current, adjusted_stake, room, original) in enumerate(remaining_eligible):
                                        additional = multiple  # Add one multiple at a time
                                        
                                        if room >= additional:
                                            new_allocation = current + additional
                                            high_tier_allocations[i] = (player_id, new_allocation, adjusted_stake, original)
                                            remaining_adjustment -= additional
                                            stake_logger.info(f"Added additional {additional} to high bettor {player_id}, now at {new_allocation}")
                                            
                                            # Update this entry for the next iteration
                                            remaining_eligible[idx] = (i, player_id, new_allocation, adjusted_stake, room - additional, original)
                                            
                                            if remaining_adjustment < multiple:
                                                break
                                        else:
                                            # Remove this entry as it has no more room
                                            remaining_eligible.pop(idx)
                                            break
                                    
                                    # If we've gone through all eligible bettors and still have adjustment, break to avoid infinite loop
                                    if remaining_adjustment >= multiple and not any(room >= multiple for _, _, _, _, room, _ in remaining_eligible):
                                        break
                
                # Store final high tier allocations
                for player_id, allocation, adjusted_stake, original_stake in high_tier_allocations:
                    allocations[player_id] = allocation
                    percentage = (allocation / original_stake) * 100
                    stake_logger.info(f"Max team high tier player {player_id} allocation: {allocation}/{original_stake} ({percentage:.1f}%)")
            
            # Verify total allocations match
            min_team_allocation = sum(allocations.get(pid, 0) for pid, _ in min_team)
            max_team_allocation = sum(allocations.get(pid, 0) for pid, _ in max_team)
            
            stake_logger.info(f"Final total allocations - Min team: {min_team_allocation}, Max team: {max_team_allocation}")
            if min_team_allocation != max_team_allocation:
                stake_logger.warning(f"Allocation mismatch! Min team: {min_team_allocation}, Max team: {max_team_allocation}")
                
                # Adjust to make totals match exactly
                if min_team_allocation > max_team_allocation:
                    # Find the player with highest bet in min team
                    min_team.sort(key=lambda x: allocations.get(x[0], 0), reverse=True)
                    player_to_adjust = min_team[0][0]
                    adjustment = min_team_allocation - max_team_allocation
                    allocations[player_to_adjust] -= adjustment
                    stake_logger.info(f"Adjusted min team player {player_to_adjust} allocation by -{adjustment} to balance teams")
                else:
                    # Find the player with highest bet in max team
                    max_team.sort(key=lambda x: allocations.get(x[0], 0), reverse=True)
                    player_to_adjust = max_team[0][0]
                    adjustment = max_team_allocation - min_team_allocation
                    allocations[player_to_adjust] -= adjustment
                    stake_logger.info(f"Adjusted max team player {player_to_adjust} allocation by -{adjustment} to balance teams")
            
            # ------------------------------------------------------
            # Phase 2: Generate Optimized Pairings
            # ------------------------------------------------------
            stake_logger.info("Phase 2: Generating optimized pairings")
            
            # Create lists of players with their allocations for each team
            min_team_players = [(pid, allocations.get(pid, 0)) for pid, _ in min_team]
            max_team_players = [(pid, allocations.get(pid, 0)) for pid, _ in max_team]
            
            # Initialize result pairs
            result_pairs = []
            
            # Initialize remaining allocations for each player
            remaining_allocations = copy.deepcopy(allocations)
            
            # Step 1: First match identical allocations
            stake_logger.info("Matching identical allocations first")
            min_allocations = {}
            max_allocations = {}
            
            # Group players by allocation amount
            for player_id, amount in min_team_players:
                if amount not in min_allocations:
                    min_allocations[amount] = []
                min_allocations[amount].append(player_id)
                
            for player_id, amount in max_team_players:
                if amount not in max_allocations:
                    max_allocations[amount] = []
                max_allocations[amount].append(player_id)
            
            # Match identical allocations
            for amount in sorted(min_allocations.keys(), reverse=True):
                if amount in max_allocations and amount >= min_stake:
                    min_players = min_allocations[amount]
                    max_players = max_allocations[amount]
                    
                    # Match as many as possible
                    pairs_to_match = min(len(min_players), len(max_players))
                    
                    for i in range(pairs_to_match):
                        min_player_id = min_players[i]
                        max_player_id = max_players[i]
                        
                        # Create stake pair
                        if min_team_id == 'A':
                            pair = StakePair(min_player_id, max_player_id, amount)
                        else:
                            pair = StakePair(max_player_id, min_player_id, amount)
                            
                        result_pairs.append(pair)
                        
                        # Update remaining allocations
                        remaining_allocations[min_player_id] -= amount
                        remaining_allocations[max_player_id] -= amount
                        
                        stake_logger.info(f"Matched identical allocations: Min player {min_player_id} with Max player {max_player_id} for {amount} tix")
            
            # Step 2: Sort the remaining players by allocation amount (descending)
            remaining_min_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in min_team if remaining_allocations.get(pid, 0) > 0]
            remaining_max_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in max_team if remaining_allocations.get(pid, 0) > 0]
            
            remaining_min_players.sort(key=lambda x: x[1], reverse=True)
            remaining_max_players.sort(key=lambda x: x[1], reverse=True)
            
            stake_logger.info(f"Remaining min team players sorted by allocation: {remaining_min_players}")
            stake_logger.info(f"Remaining max team players sorted by allocation: {remaining_max_players}")
            
            # Step 3: Match remaining players using an optimized algorithm
            remaining_min_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in min_team if remaining_allocations.get(pid, 0) > 0]
            remaining_max_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in max_team if remaining_allocations.get(pid, 0) > 0]

            stake_logger.info(f"Remaining min team players sorted by allocation: {remaining_min_players}")
            stake_logger.info(f"Remaining max team players sorted by allocation: {remaining_max_players}")

            # Sort by stake amount (descending)
            remaining_min_players.sort(key=lambda x: x[1], reverse=True)
            remaining_max_players.sort(key=lambda x: x[1], reverse=True)

            # Phase 1: First look for exact matches or complete allocation opportunities
            # This helps reduce splits by handling cases where allocations match exactly
            exact_matches = []
            for i, (min_player_id, min_remaining) in enumerate(remaining_min_players):
                for j, (max_player_id, max_remaining) in enumerate(remaining_max_players):
                    if min_remaining == max_remaining and min_remaining >= min_stake:
                        # Create stake pair for exact match
                        if min_team_id == 'A':
                            pair = StakePair(min_player_id, max_player_id, min_remaining)
                        else:
                            pair = StakePair(max_player_id, min_player_id, min_remaining)
                            
                        result_pairs.append(pair)
                        
                        # Update remaining allocations
                        remaining_allocations[min_player_id] = 0
                        remaining_allocations[max_player_id] = 0
                        
                        # Mark these players for removal
                        exact_matches.append((i, j))
                        
                        stake_logger.info(f"Exact match: Min player {min_player_id} with Max player {max_player_id} for {min_remaining} tix")

            # Remove exact matches from lists (in reverse order to maintain indices)
            for min_idx, max_idx in sorted(exact_matches, reverse=True):
                remaining_min_players.pop(min_idx)
                remaining_max_players.pop(max_idx)

            # Phase 2: Minimize number of splits by prioritizing complete allocations
            # Track which players must have split bets
            must_split_min = set()
            must_split_max = set()

            # Calculate total allocation for each team (should be equal)
            total_min_allocation = sum(amt for _, amt in remaining_min_players)
            total_max_allocation = sum(amt for _, amt in remaining_max_players)

            stake_logger.info(f"After exact matches - Min team remaining: {total_min_allocation}, Max team remaining: {total_max_allocation}")

            # Phase 3: Use a smarter greedy algorithm that prioritizes minimizing splits
            # We'll handle the largest allocations first to maintain bet sizes

            # First, fill the largest players on each side where possible
            # Priority cases:
            # 1. Largest min player against largest max player
            # 2. Ensure all min players get fully allocated
            # 3. Minimize number of players that need to be split

            remaining_pairings = []
            processed_min = set()
            processed_max = set()

            # First, try to maximize the number of complete matches
            # (where one player's entire allocation is handled by a single player)
            for i, (min_player_id, min_remaining) in enumerate(remaining_min_players):
                if min_player_id in processed_min:
                    continue
                    
                for j, (max_player_id, max_remaining) in enumerate(remaining_max_players):
                    if max_player_id in processed_max:
                        continue
                        
                    if min_remaining <= max_remaining:
                        # Can fully allocate min player
                        if min_team_id == 'A':
                            pair = StakePair(min_player_id, max_player_id, min_remaining)
                        else:
                            pair = StakePair(max_player_id, min_player_id, min_remaining)
                            
                        result_pairs.append(pair)
                        
                        # Update remaining allocations
                        remaining_allocations[min_player_id] = 0
                        remaining_allocations[max_player_id] -= min_remaining
                        
                        processed_min.add(min_player_id)
                        
                        # If max player is fully allocated, mark them processed too
                        if remaining_allocations[max_player_id] <= 0:
                            processed_max.add(max_player_id)
                        
                        stake_logger.info(f"Complete min match: Min player {min_player_id} with Max player {max_player_id} for {min_remaining} tix")
                        break
                
            # Filter processed players
            remaining_min_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in remaining_min_players 
                                    if pid not in processed_min and remaining_allocations.get(pid, 0) > 0]
            remaining_max_players = [(pid, remaining_allocations.get(pid, 0)) for pid, _ in remaining_max_players 
                                    if pid not in processed_max and remaining_allocations.get(pid, 0) > 0]

            # Sort remaining by allocation (descending)
            remaining_min_players.sort(key=lambda x: x[1], reverse=True)
            remaining_max_players.sort(key=lambda x: x[1], reverse=True)

            # Calculate remaining players that must split bets
            # If sum of all remaining max allocations < any min allocation, that min player must be split
            # Or if sum of all remaining min allocations < any max allocation, that max player must be split
            for min_player_id, min_remaining in remaining_min_players:
                total_max_remaining = sum(max_remaining for _, max_remaining in remaining_max_players)
                if min_remaining > total_max_remaining:
                    stake_logger.warning(f"Insufficient total max allocation to handle min player {min_player_id}")
                
                # Check if player needs to be split across multiple max players
                if all(min_remaining > max_remaining for _, max_remaining in remaining_max_players):
                    must_split_min.add(min_player_id)
                    stake_logger.info(f"Min player {min_player_id} must be split across multiple max players")

            # Now allocate the remaining min players, starting with those that must be split
            # This maximizes the chances that others can get full allocations
            remaining_min_players.sort(key=lambda x: (x[0] in must_split_min, x[1]), reverse=True)

            # For each remaining min player, try to allocate as efficiently as possible
            while remaining_min_players:
                min_player_id, min_remaining = remaining_min_players.pop(0)
                
                # Track this min player's pairings
                current_min_pairings = []
                
                # First try to get a perfect match if available
                perfect_match_found = False
                for j, (max_player_id, max_remaining) in enumerate(remaining_max_players):
                    if min_remaining == max_remaining and min_remaining >= min_stake:
                        # Perfect match, use it
                        if min_team_id == 'A':
                            pair = StakePair(min_player_id, max_player_id, min_remaining)
                        else:
                            pair = StakePair(max_player_id, min_player_id, min_remaining)
                            
                        result_pairs.append(pair)
                        
                        # Update remaining allocations
                        remaining_allocations[min_player_id] = 0
                        remaining_allocations[max_player_id] = 0
                        
                        # Remove this max player
                        remaining_max_players.pop(j)
                        
                        stake_logger.info(f"Perfect match: Min player {min_player_id} with Max player {max_player_id} for {min_remaining} tix")
                        perfect_match_found = True
                        break
                
                if perfect_match_found:
                    continue
                
                # Else allocate against available max players, prioritizing larger allocations first
                while min_remaining > 0 and remaining_max_players:
                    # Sort max players by allocation (descending) - prioritize using up full max allocations
                    remaining_max_players.sort(key=lambda x: x[1], reverse=True)
                    
                    max_player_id, max_remaining = remaining_max_players[0]
                    
                    match_amount = min(min_remaining, max_remaining)
                    
                    if match_amount >= min_stake:
                        # Create stake pair
                        if min_team_id == 'A':
                            pair = StakePair(min_player_id, max_player_id, match_amount)
                        else:
                            pair = StakePair(max_player_id, min_player_id, match_amount)
                            
                        result_pairs.append(pair)
                        current_min_pairings.append(pair)
                        
                        # Update allocations
                        min_remaining -= match_amount
                        remaining_allocations[min_player_id] -= match_amount
                        remaining_allocations[max_player_id] -= match_amount
                        
                        stake_logger.info(f"Matched remaining: Min player {min_player_id} with Max player {max_player_id} for {match_amount} tix")
                    
                    # Update or remove max player
                    if remaining_allocations[max_player_id] <= 0:
                        remaining_max_players.pop(0)
                    else:
                        remaining_max_players[0] = (max_player_id, remaining_allocations[max_player_id])
                
                # Check if we have any unallocated amount (shouldn't happen with balanced teams)
                if min_remaining > 0:
                    stake_logger.warning(f"Unallocated stake for min player {min_player_id}: {min_remaining}")
            
            # Final verification: ensure each player's total matches their allocation
            final_allocations = {pid: 0 for pid in allocations.keys()}
            for pair in result_pairs:
                final_allocations[pair.player_a_id] = final_allocations.get(pair.player_a_id, 0) + pair.amount
                final_allocations[pair.player_b_id] = final_allocations.get(pair.player_b_id, 0) + pair.amount
            
            stake_logger.info("Ensuring max team players get their target allocations...")

            # Recalculate max_player_allocated from the pairs we've created
            max_player_allocated = {player_id: 0 for player_id, _ in max_team}
            for pair in result_pairs:
                max_player_id = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                if max_player_id in max_player_allocated:
                    max_player_allocated[max_player_id] += pair.amount

            # Process max team players with allocation shortfall
            for max_player_id, target_allocation in allocations.items():
                # Skip players not in max team
                if max_player_id not in [p_id for p_id, _ in max_team]:
                    continue
                
                allocated = max_player_allocated.get(max_player_id, 0)
                if allocated < target_allocation:
                    shortfall = target_allocation - allocated
                    stake_logger.info(f"Max player {max_player_id} needs {shortfall} more to reach target allocation of {target_allocation}")
                    
                    # Find min team players with excess allocation beyond their targets
                    min_player_excess = {}
                    for min_player_id, min_target in allocations.items():
                        # Skip players not in min team
                        if min_player_id not in [p_id for p_id, _ in min_team]:
                            continue
                        
                        min_allocated = 0
                        for pair in result_pairs:
                            min_in_pair = pair.player_a_id if min_team_id == 'A' else pair.player_b_id
                            if min_in_pair == min_player_id:
                                min_allocated += pair.amount
                        
                        if min_allocated > min_target:
                            excess = min_allocated - min_target
                            min_player_excess[min_player_id] = excess
                    
                    # If there are min players with excess, redistribute
                    if min_player_excess:
                        for min_player_id, excess in min_player_excess.items():
                            if shortfall <= 0:
                                break
                            
                            # Find pairs between this min player and other max players
                            pairs_to_adjust = []
                            for i, pair in enumerate(result_pairs):
                                min_in_pair = pair.player_a_id if min_team_id == 'A' else pair.player_b_id
                                max_in_pair = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                                
                                if min_in_pair == min_player_id and max_in_pair != max_player_id:
                                    pairs_to_adjust.append((i, pair, max_in_pair))
                            
                            # Redistribute from these pairs to our max player
                            for i, pair, other_max_id in pairs_to_adjust:
                                if shortfall <= 0:
                                    break
                                
                                # How much can we take from this pair
                                reducible = min(pair.amount, excess, shortfall)
                                if reducible <= 0:
                                    continue
                                
                                # Reduce this pair
                                result_pairs[i] = StakePair(
                                    pair.player_a_id, 
                                    pair.player_b_id, 
                                    pair.amount - reducible
                                )
                                
                                # Create or update a pair for our max player
                                max_pair_found = False
                                for j, p in enumerate(result_pairs):
                                    max_is_a = min_team_id != 'A'  # max team is A if min team is B
                                    if ((max_is_a and p.player_a_id == max_player_id and p.player_b_id == min_player_id) or
                                        (not max_is_a and p.player_b_id == max_player_id and p.player_a_id == min_player_id)):
                                        # Update existing pair
                                        if max_is_a:
                                            result_pairs[j] = StakePair(max_player_id, min_player_id, p.amount + reducible)
                                        else:
                                            result_pairs[j] = StakePair(min_player_id, max_player_id, p.amount + reducible)
                                        max_pair_found = True
                                        break
                                
                                if not max_pair_found:
                                    # Create new pair
                                    if min_team_id == 'A':
                                        new_pair = StakePair(min_player_id, max_player_id, reducible)
                                    else:
                                        new_pair = StakePair(max_player_id, min_player_id, reducible)
                                    result_pairs.append(new_pair)
                                
                                stake_logger.info(f"Redistributed {reducible} from min player {min_player_id}'s pair with {other_max_id} to max player {max_player_id}")
                                
                                shortfall -= reducible
                                excess -= reducible
                                
                                if excess <= 0:
                                    break
                    
                    # If still not fully allocated, try to find underallocated min players
                    if shortfall > 0:
                        stake_logger.info(f"Max player {max_player_id} still needs {shortfall} more - looking for additional sources")
                        
                        # Try to find pairs with max players who are over their target
                        max_player_excess = {}
                        for other_max_id, other_target in allocations.items():
                            # Skip our player or players not in max team
                            if other_max_id == max_player_id or other_max_id not in [p_id for p_id, _ in max_team]:
                                continue
                            
                            other_allocated = 0
                            for pair in result_pairs:
                                other_in_pair = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                                if other_in_pair == other_max_id:
                                    other_allocated += pair.amount
                            
                            if other_allocated > other_target:
                                excess = other_allocated - other_target
                                max_player_excess[other_max_id] = excess
                        
                        # If there are max players with excess, redistribute
                        if max_player_excess:
                            for other_max_id, excess in max_player_excess.items():
                                if shortfall <= 0:
                                    break
                                
                                # Find pairs between other max player and min players
                                pairs_to_adjust = []
                                for i, pair in enumerate(result_pairs):
                                    min_in_pair = pair.player_a_id if min_team_id == 'A' else pair.player_b_id
                                    max_in_pair = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                                    
                                    if max_in_pair == other_max_id:
                                        pairs_to_adjust.append((i, pair, min_in_pair))
                                
                                # Redistribute from these pairs to our max player
                                for i, pair, min_player_id in pairs_to_adjust:
                                    if shortfall <= 0:
                                        break
                                    
                                    # How much can we take from this pair
                                    reducible = min(pair.amount, excess, shortfall)
                                    if reducible <= 0:
                                        continue
                                    
                                    # Reduce this pair
                                    result_pairs[i] = StakePair(
                                        pair.player_a_id, 
                                        pair.player_b_id, 
                                        pair.amount - reducible
                                    )
                                    
                                    # Create or update a pair for our max player
                                    max_pair_found = False
                                    for j, p in enumerate(result_pairs):
                                        max_is_a = min_team_id != 'A'  # max team is A if min team is B
                                        if ((max_is_a and p.player_a_id == max_player_id and p.player_b_id == min_player_id) or
                                            (not max_is_a and p.player_b_id == max_player_id and p.player_a_id == min_player_id)):
                                            # Update existing pair
                                            if max_is_a:
                                                result_pairs[j] = StakePair(max_player_id, min_player_id, p.amount + reducible)
                                            else:
                                                result_pairs[j] = StakePair(min_player_id, max_player_id, p.amount + reducible)
                                            max_pair_found = True
                                            break
                                    
                                    if not max_pair_found:
                                        # Create new pair
                                        if min_team_id == 'A':
                                            new_pair = StakePair(min_player_id, max_player_id, reducible)
                                        else:
                                            new_pair = StakePair(max_player_id, min_player_id, reducible)
                                        result_pairs.append(new_pair)
                                    
                                    stake_logger.info(f"Redistributed {reducible} from max player {other_max_id}'s pair with {min_player_id} to max player {max_player_id}")
                                    
                                    shortfall -= reducible
                                    excess -= reducible
                                    
                                    if excess <= 0:
                                        break
                                    
            # Log the final allocation for each player
            stake_logger.info("Final allocation by player:")
            for player_id in sorted(allocations.keys()):
                target = allocations.get(player_id, 0)
                actual = final_allocations.get(player_id, 0)
                original = original_stakes.get(player_id, 0)
                
                percentage = (actual / original) * 100 if original > 0 else 0
                stake_logger.info(f"Player {player_id}: {actual}/{original} tix ({percentage:.1f}%)")
                
                if actual != target:
                    stake_logger.warning(f"Player {player_id} allocation mismatch! Target: {target}, Actual: {actual}")
            
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
            
            stake_logger.info(f"Final consolidated stake pairs: {consolidated_pairs}")
            return consolidated_pairs
            
        except Exception as e:
            # Log the error
            stake_logger.error(f"Error in tiered_stakes_calculator: {str(e)}")
            # Fall back to optimized algorithm if there's an error
            stake_logger.info("Falling back to optimized algorithm due to error")
            return OptimizedStakeCalculator.calculate_stakes(team_a, team_b, stakes, min_stake, multiple)

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

    # Define upper bound for outliers (standard is 1.5 * iqr)
    upper_bound = q3 + (1.5 * iqr)

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