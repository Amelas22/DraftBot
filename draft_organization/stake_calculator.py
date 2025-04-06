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
                         stakes: Dict[str, int], min_stake: int = 10,
                         multiple: int = 10) -> List[StakePair]:
        """
        Calculate stake pairings between two teams.
        
        Args:
            team_a: List of player IDs in team A
            team_b: List of player IDs in team B
            stakes: Dictionary mapping player IDs to their max stake
            min_stake: Minimum stake amount allowed
            multiple: Round stakes to this multiple
            
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
                
                # CHANGED: Check against multiple instead of min_stake
                if bet_amount >= multiple:
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
                    # CHANGED: Log message to reflect the check against multiple
                    stake_logger.info(f"Bet amount {bet_amount} is below minimum multiple {multiple}, skipping this pairing")
                    # Keep the stakes that were not used due to minimum multiple
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
                                multiple: int = 10, cap_info: Dict[str, bool] = None) -> List[StakePair]:
        """
        Calculate stake pairings using a tiered approach that prioritizes 10/20/50 bets
        and applies proportional allocation to higher bets.
        
        This uses a two-phase approach:
        1. First determine optimal individual allocations for each player
        2. Then create efficient pairings to minimize transactions
        
        Args:
            team_a: List of player IDs in team A
            team_b: List of player IDs in team B
            stakes: Dictionary mapping player IDs to their max stake
            min_stake: Minimum stake amount allowed
            multiple: Round stakes to this multiple
            cap_info: Dictionary mapping player IDs to their bet capping preference (True/False)
        """
        try:
            # Create a deep copy of stakes to avoid modifying the original
            import copy
            stakes = copy.deepcopy(stakes)
            
            # Track original stakes before any adjustments
            original_stakes = copy.deepcopy(stakes)
            
            stake_logger.info(f"Starting tiered stake calculation with: Team A: {team_a}, Team B: {team_b}")
            stake_logger.info(f"Input stakes: {stakes}")
            stake_logger.info(f"Minimum stake: {min_stake}")
            if cap_info:
                stake_logger.info(f"Cap info: {cap_info}")
            
            # Create sorted lists of player stakes for each team
            team_a_stakes = [(player_id, stakes[player_id]) for player_id in team_a if player_id in stakes]
            team_b_stakes = [(player_id, stakes[player_id]) for player_id in team_b if player_id in stakes]
            
            # Log team stakes before any adjustments
            stake_logger.info(f"Team A stakes before cap adjustment: {team_a_stakes}")
            stake_logger.info(f"Team B stakes before cap adjustment: {team_b_stakes}")
            
            # STEP 0: Apply bet capping for players who opted in
            if cap_info:
                # Find the highest stake in team B for capping team A players
                max_stake_b = max([stake for _, stake in team_b_stakes]) if team_b_stakes else 0
                # Find the highest stake in team A for capping team B players
                max_stake_a = max([stake for _, stake in team_a_stakes]) if team_a_stakes else 0
                
                stake_logger.info(f"Highest bet on Team A: {max_stake_a} tix")
                stake_logger.info(f"Highest bet on Team B: {max_stake_b} tix")
                
                # Cap bets for team A players who opted for capping
                for i, (player_id, player_stake) in enumerate(team_a_stakes):
                    # Check if player opted for capping and their stake is higher than cap
                    if player_id in cap_info and cap_info[player_id] and player_stake > max_stake_b:
                        team_a_stakes[i] = (player_id, max_stake_b)
                        stakes[player_id] = max_stake_b  # Update the stakes dictionary
                        stake_logger.info(f"Capped Team A player {player_id} from {player_stake} to {max_stake_b} due to cap preference")
                
                # Cap bets for team B players who opted for capping
                for i, (player_id, player_stake) in enumerate(team_b_stakes):
                    # Check if player opted for capping and their stake is higher than cap
                    if player_id in cap_info and cap_info[player_id] and player_stake > max_stake_a:
                        team_b_stakes[i] = (player_id, max_stake_a)
                        stakes[player_id] = max_stake_a  # Update the stakes dictionary
                        stake_logger.info(f"Capped Team B player {player_id} from {player_stake} to {max_stake_a} due to cap preference")
                
                # Log team stakes after cap adjustments
                stake_logger.info(f"Team A stakes after cap adjustment: {team_a_stakes}")
                stake_logger.info(f"Team B stakes after cap adjustment: {team_b_stakes}")
                
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
                    rounded_allocation = max(rounded_allocation, multiple)  # CHANGED: minimum is now multiple
                    
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
            # Phase 2: Generate Optimized Pairings with Balanced Allocation
            # ------------------------------------------------------
            stake_logger.info("Phase 2: Generating balanced stake pairings")

            # Create lists of players with their allocations for each team
            min_team_players = [(pid, allocations.get(pid, 0)) for pid, _ in min_team]
            max_team_players = [(pid, allocations.get(pid, 0)) for pid, _ in max_team]

            # Create a dictionary of target allocations for faster lookup
            target_allocations = copy.deepcopy(allocations)

            # Initialize result pairs
            result_pairs = []

            # Initialize tracking of allocated amounts
            allocated = {player_id: 0 for player_id in allocations.keys()}

            # Step 1: Match identical allocations first (these are always perfect matches)
            stake_logger.info("Matching identical allocations first")
            min_by_allocation = {}
            max_by_allocation = {}

            # Group players by allocation amount
            for player_id, amount in min_team_players:
                if amount not in min_by_allocation:
                    min_by_allocation[amount] = []
                min_by_allocation[amount].append(player_id)

            for player_id, amount in max_team_players:
                if amount not in max_by_allocation:
                    max_by_allocation[amount] = []
                max_by_allocation[amount].append(player_id)

            # Match identical allocations
            for amount in sorted(min_by_allocation.keys(), reverse=True):
                if amount in max_by_allocation and amount >= multiple:  # CHANGED: check against multiple
                    min_players = min_by_allocation[amount]
                    max_players = max_by_allocation[amount]
                    
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
                        
                        # Update allocated amounts
                        allocated[min_player_id] += amount
                        allocated[max_player_id] += amount
                        
                        stake_logger.info(f"Matched identical allocations: Min player {min_player_id} with Max player {max_player_id} for {amount} tix")

            # Step 2: Prepare remaining players for balanced matching
            remaining_min = [(pid, target_allocations[pid] - allocated[pid]) 
                            for pid, _ in min_team_players 
                            if target_allocations[pid] > allocated[pid]]

            remaining_max = [(pid, target_allocations[pid] - allocated[pid]) 
                            for pid, _ in max_team_players 
                            if target_allocations[pid] > allocated[pid]]

            # Sort by remaining amount (descending)
            remaining_min.sort(key=lambda x: x[1], reverse=True)
            remaining_max.sort(key=lambda x: x[1], reverse=True)

            stake_logger.info(f"Remaining min team players: {remaining_min}")
            stake_logger.info(f"Remaining max team players: {remaining_max}")

            # Step 3: Formulate as a balanced allocation problem
            # We'll use a modified Hungarian algorithm approach - assign players greedily
            # but respect allocation limits on both sides

            # This data structure will track allocation status of all players
            allocation_status = {
                'min': {pid: {'target': target_allocations[pid], 'allocated': allocated[pid]} 
                    for pid, _ in min_team},
                'max': {pid: {'target': target_allocations[pid], 'allocated': allocated[pid]} 
                    for pid, _ in max_team}
            }

            # Define a scoring function to prioritize matches
            def match_score(min_player, max_player, min_remaining, max_remaining):
                """
                Higher score means better match. This function prioritizes:
                - Matches where one player would be completely allocated
                - Matches with largest possible stake amount
                - Matches that minimize leftover small amounts
                """
                match_amount = min(min_remaining, max_remaining)
                
                # Base score is the match amount
                score = match_amount
                
                # Bonus for exact matches (completely allocates both players)
                if min_remaining == max_remaining:
                    score += 1000
                
                # Bonus for completely allocating either player
                elif min_remaining == match_amount or max_remaining == match_amount:
                    score += 500
                    
                # Penalty for leaving small remainder that might be below multiple
                remainder = abs(min_remaining - max_remaining)
                if 0 < remainder < multiple:  # CHANGED: check against multiple
                    score -= 300
                
                return score

            # Process until all players are matched or no more valid matches exist
            while remaining_min and remaining_max:
                # Find the best match among remaining players
                best_score = -float('inf')
                best_match = None
                
                for i, (min_player, min_remaining) in enumerate(remaining_min):
                    for j, (max_player, max_remaining) in enumerate(remaining_max):
                        match_amount = min(min_remaining, max_remaining)
                        
                        # Skip if match amount is below minimum multiple
                        if match_amount < multiple:  # CHANGED: check against multiple
                            continue
                            
                        score = match_score(min_player, max_player, min_remaining, max_remaining)
                        
                        if score > best_score:
                            best_score = score
                            best_match = (i, j, match_amount)
                
                # If no valid match found, break
                if best_match is None:
                    stake_logger.info("No more valid matches found")
                    break
                    
                # Create the stake pair for the best match
                i, j, match_amount = best_match
                min_player, min_remaining = remaining_min[i]
                max_player, max_remaining = remaining_max[j]
                
                if min_team_id == 'A':
                    pair = StakePair(min_player, max_player, match_amount)
                else:
                    pair = StakePair(max_player, min_player, match_amount)
                    
                result_pairs.append(pair)
                
                # Update allocated amounts
                allocated[min_player] += match_amount
                allocated[max_player] += match_amount
                
                stake_logger.info(f"Matched: Min player {min_player} with Max player {max_player} for {match_amount} tix")
                
                # Update remaining amounts
                min_new_remaining = min_remaining - match_amount
                max_new_remaining = max_remaining - match_amount
                
                # Remove or update min player
                if min_new_remaining < multiple:  # CHANGED: check against multiple
                    # If remaining amount is below multiple, don't consider for future matches
                    remaining_min.pop(i)
                else:
                    remaining_min[i] = (min_player, min_new_remaining)
                
                # Remove or update max player
                if max_new_remaining < multiple:  # CHANGED: check against multiple
                    remaining_max.pop(j)
                else:
                    remaining_max[j] = (max_player, max_new_remaining)
                
            # Step 4: Process any remaining players with small allocations (below multiple)
            if remaining_min or remaining_max:
                stake_logger.info("Processing remaining small allocations:")
                
                small_min = [(pid, amt) for pid, amt in remaining_min if amt > 0]
                small_max = [(pid, amt) for pid, amt in remaining_max if amt > 0]
                
                if small_min:
                    stake_logger.info(f"Min team small allocations: {small_min}")
                if small_max:
                    stake_logger.info(f"Max team small allocations: {small_max}")
                
                # Try to combine small allocations to meet multiple requirement
                # or add to existing stake pairs
                
                # First try to handle min team small allocations
                for min_player, min_amount in small_min:
                    # Look for existing pairs with this min player
                    for i, pair in enumerate(result_pairs):
                        min_in_pair = pair.player_a_id if min_team_id == 'A' else pair.player_b_id
                        max_in_pair = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                        
                        if min_in_pair == min_player:
                            # Add to existing pair if the max player still has room
                            max_target = target_allocations[max_in_pair]
                            max_current = allocated[max_in_pair]
                            
                            if max_current < max_target:
                                # Can add to this pair
                                add_amount = min(min_amount, max_target - max_current)
                                
                                if min_team_id == 'A':
                                    result_pairs[i] = StakePair(min_player, max_in_pair, pair.amount + add_amount)
                                else:
                                    result_pairs[i] = StakePair(max_in_pair, min_player, pair.amount + add_amount)
                                
                                allocated[min_player] += add_amount
                                allocated[max_in_pair] += add_amount
                                min_amount -= add_amount
                                
                                stake_logger.info(f"Added {add_amount} to existing pair: {min_player} with {max_in_pair}")
                                
                                if min_amount <= 0:
                                    break
                
                # Then try to handle max team small allocations
                for max_player, max_amount in small_max:
                    # Look for existing pairs with this max player
                    for i, pair in enumerate(result_pairs):
                        min_in_pair = pair.player_a_id if min_team_id == 'A' else pair.player_b_id
                        max_in_pair = pair.player_b_id if min_team_id == 'A' else pair.player_a_id
                        
                        if max_in_pair == max_player:
                            # Add to existing pair if the min player still has room
                            min_target = target_allocations[min_in_pair]
                            min_current = allocated[min_in_pair]
                            
                            if min_current < min_target:
                                # Can add to this pair
                                add_amount = min(max_amount, min_target - min_current)
                                
                                if min_team_id == 'A':
                                    result_pairs[i] = StakePair(min_in_pair, max_player, pair.amount + add_amount)
                                else:
                                    result_pairs[i] = StakePair(max_player, min_in_pair, pair.amount + add_amount)
                                
                                allocated[max_player] += add_amount
                                allocated[min_in_pair] += add_amount
                                max_amount -= add_amount
                                
                                stake_logger.info(f"Added {add_amount} to existing pair: {max_player} with {min_in_pair}")
                                
                                if max_amount <= 0:
                                    break

            # Step 5: Verify allocation status
            total_min_allocated = sum(allocated[pid] for pid, _ in min_team)
            total_max_allocated = sum(allocated[pid] for pid, _ in max_team)
            total_min_target = sum(target_allocations[pid] for pid, _ in min_team)
            total_max_target = sum(target_allocations[pid] for pid, _ in max_team)

            stake_logger.info(f"Min team: allocated {total_min_allocated} of {total_min_target}")
            stake_logger.info(f"Max team: allocated {total_max_allocated} of {total_max_target}")

            # Set final_allocations to our tracked allocations for compatibility with existing code
            final_allocations = allocated

            # Track allocation fulfillment for each player
            for player_id in sorted(allocations.keys()):
                target = target_allocations.get(player_id, 0)
                actual = allocated.get(player_id, 0)
                fulfillment = (actual / target * 100) if target > 0 else 0
                
                if actual < target:
                    stake_logger.warning(f"Player {player_id} allocation incomplete: {actual}/{target} ({fulfillment:.1f}%)")
                else:
                    stake_logger.info(f"Player {player_id} allocation complete: {actual}/{target} ({fulfillment:.1f}%)")

            stake_logger.info("Running Post-Processing Check...")

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
                                 multiple: int = 10, use_optimized: bool = False,
                                 cap_info: Dict[str, bool] = None) -> List[StakePair]:
    """
    Calculate stake pairings using either the original or optimized algorithm.
    
    Args:
        team_a: List of player IDs in team A
        team_b: List of player IDs in team B
        stakes: Dictionary mapping player IDs to their max stake
        min_stake: Minimum stake amount allowed (from user input in /dynamic_stake)
        multiple: Round stakes to this multiple (5 or 10)
        use_optimized: Whether to use the optimized algorithm
        cap_info: Dictionary mapping player IDs to their bet capping preference (True/False)
        
    Returns:
        List of StakePair objects representing the stake assignments
    """
    # Determine the actual minimum bet in the sign-up list
    actual_min_bet = min(stakes.values())

    if cap_info:
        stake_logger.info(f"Using tiered stake calculation with bet capping. Min stake={min_stake}, using actual min bet={actual_min_bet}")
        stake_logger.info(f"Capping preferences: {cap_info}")
    else:
        stake_logger.info(f"Using tiered stake calculation without bet capping. Min stake={min_stake}, using actual min bet={actual_min_bet}")
        
    return StakeCalculator.tiered_stakes_calculator(team_a, team_b, stakes, actual_min_bet, multiple, cap_info)

    
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
        stake_logger.info(f"Max Team: {max_team} (total: {max_team_total}")
        
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
                rounded_allocation = max(rounded_allocation, multiple)  # CHANGED: minimum is now multiple
                
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
                    # Distribute additional capacity fairly across multiple players
                    # Find players who have room for adjustment
                    eligible_players = []
                    for i, (player_id, current_allocation) in enumerate(above_min_allocations):
                        original_max = next(stake for pid, stake in above_min_players if pid == player_id)
                        room_left = original_max - current_allocation
                        if room_left >= multiple:
                            eligible_players.append((i, player_id, current_allocation, original_max, room_left))
                    
                    # Calculate how many multiples of adjustment we need to distribute
                    adjustment_units = adjustment_needed // multiple
                    
                    # Count eligible players for adjustment
                    num_eligible = len(eligible_players)
                    
                    if num_eligible > 0:
                        stake_logger.info(f"Distributing positive adjustment of {adjustment_needed} across eligible players (in multiples of {multiple})")
                        
                        # First pass: Apply one multiple of increase to as many players as needed
                        players_to_adjust = min(adjustment_units, num_eligible)
                        adjustment_applied = 0
                        
                        # Sort by room_left (most room first) to prioritize players with more capacity
                        eligible_players.sort(key=lambda x: x[4], reverse=True)
                        
                        for j in range(players_to_adjust):
                            i, player_id, current_allocation, original_max, room_left = eligible_players[j]
                            
                            if room_left >= multiple:
                                increase = multiple
                                new_allocation = current_allocation + increase
                                above_min_allocations[i] = (player_id, new_allocation)
                                adjustment_needed -= increase
                                adjustment_applied += increase
                                eligible_players[j] = (i, player_id, new_allocation, original_max, room_left - increase)
                                stake_logger.info(f"Added {increase} to bettor {player_id}, now at {new_allocation}")
                        
                        # Second pass: If we need more adjustments, add another multiple to players who have room
                        remaining_adjustment = adjustment_needed
                        
                        while remaining_adjustment >= multiple:
                            adjusted_player = False
                            
                            for j, (i, player_id, current_allocation, original_max, room_left) in enumerate(eligible_players):
                                if room_left >= multiple:
                                    increase = multiple
                                    new_allocation = current_allocation + increase
                                    above_min_allocations[i] = (player_id, new_allocation)
                                    remaining_adjustment -= increase
                                    eligible_players[j] = (i, player_id, new_allocation, original_max, room_left - increase)
                                    stake_logger.info(f"Added additional {increase} to bettor {player_id}, now at {new_allocation}")
                                    adjusted_player = True
                                    break
                            
                            # If no more players can be adjusted, break
                            if not adjusted_player:
                                stake_logger.warning(f"Could not distribute remaining adjustment of {remaining_adjustment}")
                                break
                    else:
                        stake_logger.warning(f"No eligible players for positive adjustment of {adjustment_needed}")
                
                elif adjustment_needed < 0:
                    # Distribute negative adjustment fairly across multiple players
                    # Sort players by their original max stake (but only include those above min_stake)
                    sorted_allocations = []
                    for i, (player_id, current_allocation) in enumerate(above_min_allocations):
                        original_max = next(stake for pid, stake in above_min_players if pid == player_id)
                        # Only include players who aren't already at the minimum multiple
                        if current_allocation > multiple:  # CHANGED: check against multiple
                            sorted_allocations.append((i, player_id, current_allocation, original_max))
                    
                    # Distribute negative adjustment fairly across multiple players
                    # Sort players by their original max stake (but only include those above min_stake)
                    sorted_allocations = []
                    for i, (player_id, current_allocation) in enumerate(above_min_allocations):
                        original_max = next(stake for pid, stake in above_min_players if pid == player_id)
                        # Only include players who aren't already at the minimum multiple
                        if current_allocation > multiple:  # CHANGED: check against multiple
                            sorted_allocations.append((i, player_id, current_allocation, original_max))
                    
                    # Calculate how many multiples of adjustment we need to distribute
                    adjustment_units = abs(adjustment_needed) // multiple
                    
                    # Count eligible players for adjustment
                    num_eligible = len(sorted_allocations)
                    
                    if num_eligible > 0:
                        stake_logger.info(f"Distributing negative adjustment of {abs(adjustment_needed)} across eligible players (in multiples of {multiple})")
                        
                        # Sort by allocation (highest first)
                        sorted_allocations.sort(key=lambda x: x[2], reverse=True)
                        
                        # First pass: Apply one multiple of reduction to as many players as needed
                        players_to_adjust = min(adjustment_units, num_eligible)
                        adjustment_applied = 0
                        
                        for j in range(players_to_adjust):
                            i, player_id, current_allocation, original_max = sorted_allocations[j]
                            max_reduction = current_allocation - multiple  # CHANGED: check against multiple
                            
                            if max_reduction >= multiple:
                                reduction = multiple
                                new_allocation = current_allocation - reduction
                                above_min_allocations[i] = (player_id, new_allocation)
                                adjustment_needed += reduction
                                adjustment_applied += reduction
                                sorted_allocations[j] = (i, player_id, new_allocation, original_max)
                                stake_logger.info(f"Removed {reduction} from bettor {player_id}, now at {new_allocation}")
                        
                        # Second pass: If we need more adjustments, add another multiple to players who can take it
                        remaining_adjustment = abs(adjustment_needed) - adjustment_applied
                        
                        while remaining_adjustment >= multiple:
                            adjusted_player = False
                            
                            for j, (i, player_id, current_allocation, original_max) in enumerate(sorted_allocations):
                                max_reduction = current_allocation - multiple  # CHANGED: check against multiple
                                
                                if max_reduction >= multiple:
                                    reduction = multiple
                                    new_allocation = current_allocation - reduction
                                    above_min_allocations[i] = (player_id, new_allocation)
                                    adjustment_needed += reduction
                                    remaining_adjustment -= reduction
                                    sorted_allocations[j] = (i, player_id, new_allocation, original_max)
                                    stake_logger.info(f"Removed additional {reduction} from bettor {player_id}, now at {new_allocation}")
                                    adjusted_player = True
                                    break
                            
                            # If no more players can be adjusted, break
                            if not adjusted_player:
                                stake_logger.warning(f"Could not distribute remaining adjustment of {remaining_adjustment}")
                                break
                    else:
                        stake_logger.warning(f"No eligible players for negative adjustment of {adjustment_needed}")
            
            # Combine allocations for all players
            all_allocations = above_min_allocations + [(pid, min(stake, min_stake)) for pid, stake in min_stake_players]
        else:
            # If all players are min stake, just allocate min stake to everyone
            all_allocations = [(pid, min(stake, min_stake)) for pid, stake in min_stake_players]
            
            stake_logger.info(f"All players at min stake: {all_allocations}")
        
        stake_logger.info(f"Final max team allocations: {all_allocations}")
        
        # Step 7: Create stake pairs with optimized matching to minimize transactions
        stake_logger.info("Creating stake pairs with transaction minimization...")

        import copy
        result_pairs = []

        # Create dictionaries for easier lookup
        max_team_allocations = {player_id: allocation for player_id, allocation in all_allocations}
            
        # Track how much has been allocated to each player
        min_team_allocations = {player_id: stake for player_id, stake in min_team}
        min_allocated = {player_id: 0 for player_id, _ in min_team}
        max_allocated = {player_id: 0 for player_id, _ in all_allocations}

        # Track which players still need allocation
        remaining_min = [(pid, min_team_allocations[pid] - min_allocated[pid]) 
                        for pid, _ in min_team 
                        if min_team_allocations[pid] > min_allocated[pid]]

        remaining_max = [(pid, max_team_allocations[pid] - max_allocated[pid]) 
                        for pid, allocation in all_allocations 
                        if max_team_allocations[pid] > max_allocated[pid]]

        # Sort by remaining allocation (descending)
        remaining_min.sort(key=lambda x: x[1], reverse=True)
        remaining_max.sort(key=lambda x: x[1], reverse=True)

        stake_logger.info(f"Remaining min team allocations: {remaining_min}")
        stake_logger.info(f"Remaining max team allocations: {remaining_max}")

        # First pass: Look for exact matches - one at a time to prevent over-allocation
        while True:
            exact_match_found = False
            
            for i, (min_player, min_remaining) in enumerate(remaining_min):
                for j, (max_player, max_remaining) in enumerate(remaining_max):
                    if min_remaining == max_remaining and min_remaining >= multiple:  # CHANGED: check against multiple
                        # Exact match - create pair
                        if is_team_a_min:
                            pair = StakePair(min_player, max_player, min_remaining)
                        else:
                            pair = StakePair(max_player, min_player, min_remaining)
                            
                        result_pairs.append(pair)
                        min_allocated[min_player] += min_remaining
                        max_allocated[max_player] += max_remaining
                        
                        stake_logger.info(f"Exact match: Min player {min_player} with Max player {max_player} for {min_remaining}")
                        
                        # Update remaining lists
                        new_remaining_min = []
                        new_remaining_max = []
                        
                        # Update min players
                        for idx, (player, remaining) in enumerate(remaining_min):
                            if idx == i:  # This is the player we just matched
                                # Skip it since it's fully allocated
                                pass
                            else:
                                new_remaining_min.append((player, remaining))
                        
                        # Update max players
                        for idx, (player, remaining) in enumerate(remaining_max):
                            if idx == j:  # This is the player we just matched
                                # Skip it since it's fully allocated
                                pass
                            else:
                                new_remaining_max.append((player, remaining))
                        
                        # Replace the lists
                        remaining_min = new_remaining_min
                        remaining_max = new_remaining_max
                        
                        exact_match_found = True
                        break  # Exit the inner loop
                    
                if exact_match_found:
                    break  # Exit the outer loop
                    
            if not exact_match_found:
                break  # No more exact matches found, exit the while loop

        # Second pass: Try to minimize the number of transactions by prioritizing matches
        # that fully allocate a player (especially from the min team)
        while remaining_min and remaining_max:
            # Find best match to minimize transactions
            best_match = None
            best_score = -1
            
            for i, (min_player, min_remaining) in enumerate(remaining_min):
                for j, (max_player, max_remaining) in enumerate(remaining_max):
                    match_amount = min(min_remaining, max_remaining)
                    
                    if match_amount < multiple:  # CHANGED: check against multiple
                        continue
                        
                    # Calculate match score - prioritize:
                    # 1. Exact matches (highest priority)
                    # 2. Matches that fully allocate a min player
                    # 3. Matches that fully allocate any player
                    # 4. Larger matches
                    
                    score = match_amount  # Base score is the match amount
                    
                    # Exact match bonus
                    if min_remaining == max_remaining:
                        score += 10000
                        
                    # Min player full allocation bonus
                    if match_amount == min_remaining:
                        score += 5000
                        
                    # Max player full allocation bonus
                    if match_amount == max_remaining:
                        score += 2000
                        
                    # Avoid creating tiny remainders that can't be matched
                    remainder_min = min_remaining - match_amount
                    remainder_max = max_remaining - match_amount
                    
                    if 0 < remainder_min < multiple:  # CHANGED: check against multiple
                        score -= 3000
                        
                    if 0 < remainder_max < multiple:  # CHANGED: check against multiple
                        score -= 1000
                        
                    if score > best_score:
                        best_score = score
                        best_match = (i, j, match_amount)
            
            if best_match is None:
                stake_logger.info("No more valid matches found")
                break
                
            # Create pair for best match
            i, j, match_amount = best_match
            min_player, min_remaining = remaining_min[i]
            max_player, max_remaining = remaining_max[j]
            
            if is_team_a_min:
                pair = StakePair(min_player, max_player, match_amount)
            else:
                pair = StakePair(max_player, min_player, match_amount)
                
            result_pairs.append(pair)
            
            # Update allocated amounts
            min_allocated[min_player] += match_amount
            max_allocated[max_player] += match_amount
            
            stake_logger.info(f"Matched: Min player {min_player} with Max player {max_player} for {match_amount}")
            
            # Create new lists instead of modifying during iteration
            new_remaining_min = []
            new_remaining_max = []
            
            # Process all remaining min players
            for idx, (player, remaining) in enumerate(remaining_min):
                if idx == i:  # This is the player we just matched
                    new_remaining = remaining - match_amount
                    if new_remaining >= multiple:  # CHANGED: check against multiple
                        new_remaining_min.append((player, new_remaining))
                else:
                    new_remaining_min.append((player, remaining))
            
            # Process all remaining max players
            for idx, (player, remaining) in enumerate(remaining_max):
                if idx == j:  # This is the player we just matched
                    new_remaining = remaining - match_amount
                    if new_remaining >= multiple:  # CHANGED: check against multiple
                        new_remaining_max.append((player, new_remaining))
                else:
                    new_remaining_max.append((player, remaining))
            
            # Replace the lists
            remaining_min = new_remaining_min
            remaining_max = new_remaining_max

        # Handle any remaining tiny allocations by adding to existing pairs
        if remaining_min or remaining_max:
            tiny_min = [(pid, amt) for pid, amt in remaining_min if amt > 0]
            tiny_max = [(pid, amt) for pid, amt in remaining_max if amt > 0]
            
            if tiny_min:
                stake_logger.info(f"Tiny min allocations left: {tiny_min}")
                
                for min_player, min_amount in tiny_min:
                    # Try to add to an existing pair with this min player
                    added = False
                    
                    for i, pair in enumerate(result_pairs):
                        min_in_pair = pair.player_a_id if is_team_a_min else pair.player_b_id
                        max_in_pair = pair.player_b_id if is_team_a_min else pair.player_a_id
                        
                        if min_in_pair == min_player:
                            # Can we add to this pair?
                            max_target = max_team_allocations.get(max_in_pair, 0)
                            max_current = max_allocated.get(max_in_pair, 0)
                            
                            if max_current < max_target:
                                add_amount = min(min_amount, max_target - max_current)
                                
                                # Update the pair
                                if is_team_a_min:
                                    result_pairs[i] = StakePair(min_player, max_in_pair, pair.amount + add_amount)
                                else:
                                    result_pairs[i] = StakePair(max_in_pair, min_player, pair.amount + add_amount)
                                    
                                min_allocated[min_player] += add_amount
                                max_allocated[max_in_pair] += add_amount
                                min_amount -= add_amount
                                
                                stake_logger.info(f"Added {add_amount} to existing pair: Min {min_player} with Max {max_in_pair}")
                                
                                if min_amount <= 0:
                                    added = True
                                    break
                    
                    # If not added to an existing pair and amount is substantial, create a new pair
                    if not added and min_amount >= multiple:  # CHANGED: check against multiple
                        for max_player, max_allocation in max_team_allocations.items():
                            max_current = max_allocated.get(max_player, 0)
                            
                            if max_current < max_allocation:
                                add_amount = min(min_amount, max_allocation - max_current)
                                
                                if add_amount >= multiple:  # CHANGED: check against multiple
                                    # Create a new pair
                                    if is_team_a_min:
                                        new_pair = StakePair(min_player, max_player, add_amount)
                                    else:
                                        new_pair = StakePair(max_player, min_player, add_amount)
                                        
                                    result_pairs.append(new_pair)
                                    min_allocated[min_player] += add_amount
                                    max_allocated[max_player] += add_amount
                                    min_amount -= add_amount
                                    
                                    stake_logger.info(f"Created new pair for tiny amt: Min {min_player} with Max {max_player} for {add_amount}")
                                    
                                    if min_amount <= 0:
                                        break
            
            if tiny_max:
                stake_logger.info(f"Tiny max allocations left: {tiny_max}")
                # Similar handling for tiny max allocations...

        # Final verification step
        for min_player, target in min_team_allocations.items():
            actual = min_allocated.get(min_player, 0)
            
            if actual < target:
                stake_logger.warning(f"Min player {min_player} allocation incomplete: {actual}/{target}")
            else:
                stake_logger.info(f"Min player {min_player} fully allocated: {actual}/{target}")
                
        for max_player, target in max_team_allocations.items():
            actual = max_allocated.get(max_player, 0)
            
            if actual < target:
                stake_logger.warning(f"Max player {max_player} allocation incomplete: {actual}/{target}")
            else:
                stake_logger.info(f"Max player {max_player} fully allocated: {actual}/{target}")

        # Consolidate multiple bets between the same players
        stake_logger.info("Consolidating multiple bets between same players...")
        consolidated_pairs = []
        pair_map = {}

        for pair in result_pairs:
            # Create a unique key for each player pair
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

        stake_logger.info(f"Final consolidated pairs: {consolidated_pairs}")
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