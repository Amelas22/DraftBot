import random
import io
import sys
import pandas as pd
from datetime import datetime
from loguru import logger
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from stake_calculator import calculate_stakes_with_strategy, StakePair

# Define number of simulations to run
NUM_SIMULATIONS = 5

# Setup custom log handler to capture logs
class LogCapture:
    def __init__(self):
        self.logs = io.StringIO()
        self.handler_id = None

    def start(self):
        self.logs = io.StringIO()
        self.handler_id = logger.add(self.logs, level="INFO", filter=lambda record: record["name"] == "stake_calculator")

    def stop(self):
        if self.handler_id:
            logger.remove(self.handler_id)
            self.handler_id = None

    def get_logs(self):
        return self.logs.getvalue()


def get_player_stakes():
    """
    Get user input for player stakes.
    Returns a dictionary mapping player IDs to stake amounts.
    """
    print("Enter player stakes:")
    
    players = {}
    player_count = 0
    
    while True:
        player_input = input(f"Player {player_count + 1} name and stake (e.g., 'Alice 100') or 'done' to finish: ")
        
        if player_input.lower() == 'done':
            break
            
        try:
            parts = player_input.split(' ')
            if len(parts) != 2:
                print("Invalid format. Please enter name and stake separated by space.")
                continue
                
            name, stake = parts
            stake = int(stake)
            
            # Use name as player ID
            player_id = name
            players[player_id] = stake
            player_count += 1
            
            # For testing, we'll allow 6 or 8 players
            if player_count >= 8:
                break
        except ValueError:
            print("Invalid stake amount. Please enter a number.")
    
    # Validate we have enough players
    if player_count < 6:
        print("Need at least 6 players to run simulations.")
        return None
    
    # If odd number of players between 6 and 8, ask for one more
    if player_count % 2 != 0:
        print("Need an even number of players. Please add one more.")
        return get_player_stakes()
        
    return players


def randomize_teams(player_ids):
    """
    Randomly split players into two equal teams.
    """
    # Convert to list and shuffle
    players = list(player_ids)
    random.shuffle(players)
    
    # Split into two equal teams
    mid_point = len(players) // 2
    team_a = players[:mid_point]
    team_b = players[mid_point:]
    
    return team_a, team_b


def run_stake_simulations(players, min_stake=10, multiple=10):
    """
    Run multiple stake calculation simulations with different team assignments.
    Returns simulation results and captured logs.
    """
    results = []
    all_logs = []
    original_logs = []
    
    log_capture = LogCapture()
    player_ids = list(players.keys())
    
    for i in range(NUM_SIMULATIONS):
        print(f"Running simulation {i+1}...")
        
        # Randomize teams
        team_a, team_b = randomize_teams(player_ids)
        
        # ----- Run Optimized Algorithm -----
        # Start log capture
        log_capture.start()
        
        # Run stake calculation
        optimized_pairs = calculate_stakes_with_strategy(
            team_a=team_a,
            team_b=team_b,
            stakes=players,
            min_stake=min_stake,
            multiple=multiple,
            use_optimized=True
        )
        
        # Stop log capture
        log_capture.stop()
        optimized_log = log_capture.get_logs()
        
        # ----- Run Original Algorithm -----
        # Start log capture
        log_capture.start()
        
        # Run stake calculation
        original_pairs = calculate_stakes_with_strategy(
            team_a=team_a,
            team_b=team_b,
            stakes=players,
            min_stake=min_stake,
            multiple=multiple,
            use_optimized=False
        )
        
        # Stop log capture
        log_capture.stop()
        original_log = log_capture.get_logs()
        
        # Calculate total bets per player for both algorithms
        optimized_player_bets = {}
        original_player_bets = {}
        
        # Calculate totals for optimized algorithm
        for pair in optimized_pairs:
            optimized_player_bets[pair.player_a_id] = optimized_player_bets.get(pair.player_a_id, 0) + pair.amount
            optimized_player_bets[pair.player_b_id] = optimized_player_bets.get(pair.player_b_id, 0) + pair.amount
        
        # Calculate totals for original algorithm
        for pair in original_pairs:
            original_player_bets[pair.player_a_id] = original_player_bets.get(pair.player_a_id, 0) + pair.amount
            original_player_bets[pair.player_b_id] = original_player_bets.get(pair.player_b_id, 0) + pair.amount
        
        # Format player bet summaries
        optimized_bet_summary = []
        original_bet_summary = []
        
        for player_id in sorted(players.keys()):
            max_stake = players[player_id]
            optimized_bet = optimized_player_bets.get(player_id, 0)
            original_bet = original_player_bets.get(player_id, 0)
            
            # Calculate bet percentages
            opt_pct = (optimized_bet / max_stake) * 100 if max_stake > 0 else 0
            orig_pct = (original_bet / max_stake) * 100 if max_stake > 0 else 0
            
            optimized_bet_summary.append(f"{player_id}: {optimized_bet}/{max_stake} ({opt_pct:.1f}%)")
            original_bet_summary.append(f"{player_id}: {original_bet}/{max_stake} ({orig_pct:.1f}%)")
        
        # Store results
        sim_result = {
            'simulation': i+1,
            'team_a': [f"{p} ({players[p]} tix)" for p in team_a],
            'team_b': [f"{p} ({players[p]} tix)" for p in team_b],
            'optimized_stake_pairs': [f"{p.player_a_id} vs {p.player_b_id}: {p.amount} tix" for p in optimized_pairs],
            'original_stake_pairs': [f"{p.player_a_id} vs {p.player_b_id}: {p.amount} tix" for p in original_pairs],
            'optimized_total_stake': sum(p.amount for p in optimized_pairs),
            'original_total_stake': sum(p.amount for p in original_pairs),
            'optimized_num_pairs': len(optimized_pairs),
            'original_num_pairs': len(original_pairs),
            'optimized_player_bets': optimized_player_bets,
            'original_player_bets': original_player_bets,
            'optimized_bet_summary': optimized_bet_summary,
            'original_bet_summary': original_bet_summary
        }
        
        results.append(sim_result)
        all_logs.append(optimized_log)
        original_logs.append(original_log)
    
    return results, all_logs, original_logs


def write_to_excel(players, results, optimized_logs, original_logs):
    """
    Write simulation results to Excel file.
    """
    # Create a new workbook
    wb = openpyxl.Workbook()
    
    # Remove default sheet
    default_sheet = wb.active
    wb.remove(default_sheet)
    
    # Add input sheet
    input_sheet = wb.create_sheet("Player Inputs")
    input_sheet.append(["Player", "Max Stake"])
    
    for player, stake in sorted(players.items(), key=lambda x: x[1], reverse=True):
        input_sheet.append([player, stake])
    
    # Format headers
    header_fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
    header_font = Font(bold=True)
    
    for cell in input_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
    
    # Add summary sheet
    summary_sheet = wb.create_sheet("Summary", 0)  # Make it the first sheet
    summary_sheet.append([
        "Simulation", 
        "Optimized Total Stake", 
        "Original Total Stake", 
        "Difference",
        "Optimized # Txns",
        "Original # Txns",
        "Txns Saved",
        "Team A", 
        "Team B"
    ])
    
    # Format header
    for cell in summary_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
    
    # Add comparison sheet
    comparison_sheet = wb.create_sheet("Algorithm Comparison")
    comparison_sheet.append([
        "Simulation", 
        "Team Configuration", 
        "Comparison Total Bets (Max Bet: Original / Optimized)",
        "Optimized Stake Pairs",
        "Optimized Total",
        "Original Stake Pairs",
        "Original Total"
    ])
    
    # Format header
    for cell in comparison_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
    
    # Add results for each simulation
    for i, (result, opt_log, orig_log) in enumerate(zip(results, optimized_logs, original_logs)):
        # Create simulation sheet for optimized algorithm
        sim_sheet = wb.create_sheet(f"Sim {i+1} Optimized")
        
        # Add team information
        sim_sheet.append(["Team A (sorted by stake)"])
        # Sort team A by stake amount
        team_a_with_stakes = [(p.split(" (")[0], int(p.split("(")[1].split(" ")[0])) for p in result['team_a']]
        team_a_with_stakes.sort(key=lambda x: x[1], reverse=True)
        team_a_sorted = [f"{p} ({s} tix)" for p, s in team_a_with_stakes]
        for player in team_a_sorted:
            sim_sheet.append([player])
        
        sim_sheet.append([])  # Empty row
        
        sim_sheet.append(["Team B (sorted by stake)"])
        # Sort team B by stake amount
        team_b_with_stakes = [(p.split(" (")[0], int(p.split("(")[1].split(" ")[0])) for p in result['team_b']]
        team_b_with_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_sorted = [f"{p} ({s} tix)" for p, s in team_b_with_stakes]
        for player in team_b_sorted:
            sim_sheet.append([player])
        
        sim_sheet.append([])  # Empty row
        
        # Add optimized stake pairs
        sim_sheet.append(["Optimized Stake Pairs"])
        for pair in result['optimized_stake_pairs']:
            sim_sheet.append([pair])
        
        sim_sheet.append([])  # Empty row
        sim_sheet.append(["Total Stake", result['optimized_total_stake']])
        sim_sheet.append(["Number of Transactions", result['optimized_num_pairs']])
        
        # Add player bet details
        sim_sheet.append([])  # Empty row
        sim_sheet.append(["Player Bet Details"])
        
        # Format as a table
        sim_sheet.append(["Player", "Max Stake", "Allocated", "Percentage"])
        
        for player_id in sorted(players.keys()):
            max_stake = players[player_id]
            allocated = result['optimized_player_bets'].get(player_id, 0)
            percentage = (allocated / max_stake * 100) if max_stake > 0 else 0
            sim_sheet.append([player_id, max_stake, allocated, f"{percentage:.1f}%"])
        
        # Format headers
        for row_idx in [1, 4, 8, len(team_a_sorted) + len(team_b_sorted) + 8]:
            try:
                for cell in sim_sheet[row_idx]:
                    cell.fill = header_fill
                    cell.font = header_font
            except IndexError:
                # Skip if row doesn't exist
                pass
        
        # Add original algorithm results
        orig_sheet = wb.create_sheet(f"Sim {i+1} Original")
        
        # Add team information (same teams as optimized)
        orig_sheet.append(["Team A (sorted by stake)"])
        for player in team_a_sorted:
            orig_sheet.append([player])
        
        orig_sheet.append([])  # Empty row
        
        orig_sheet.append(["Team B (sorted by stake)"])
        for player in team_b_sorted:
            orig_sheet.append([player])
        
        orig_sheet.append([])  # Empty row
        
        # Add original stake pairs
        orig_sheet.append(["Original Stake Pairs"])
        for pair in result['original_stake_pairs']:
            orig_sheet.append([pair])
        
        orig_sheet.append([])  # Empty row
        orig_sheet.append(["Total Stake", result['original_total_stake']])
        orig_sheet.append(["Number of Transactions", result['original_num_pairs']])
        
        # Add player bet details
        orig_sheet.append([])  # Empty row
        orig_sheet.append(["Player Bet Details"])
        
        # Format as a table
        orig_sheet.append(["Player", "Max Stake", "Allocated", "Percentage"])
        
        for player_id in sorted(players.keys()):
            max_stake = players[player_id]
            allocated = result['original_player_bets'].get(player_id, 0)
            percentage = (allocated / max_stake * 100) if max_stake > 0 else 0
            orig_sheet.append([player_id, max_stake, allocated, f"{percentage:.1f}%"])
        
        # Format headers
        for row_idx in [1, 4, 8, len(team_a_sorted) + len(team_b_sorted) + 8]:
            try:
                for cell in orig_sheet[row_idx]:
                    cell.fill = header_fill
                    cell.font = header_font
            except IndexError:
                # Skip if row doesn't exist
                pass
        
        # Add logs
        log_sheet = wb.create_sheet(f"Sim {i+1} Logs")
        
        # Split log by sections
        log_sheet.append(["Optimized Algorithm Logs"])
        log_lines = opt_log.strip().split('\n')
        for line in log_lines:
            log_sheet.append([line])
        
        log_sheet.append([])  # Empty row
        log_sheet.append(["Original Algorithm Logs"])
        orig_log_lines = orig_log.strip().split('\n')
        for line in orig_log_lines:
            log_sheet.append([line])
        
        # Format headers
        log_sheet.cell(row=1, column=1).fill = header_fill
        log_sheet.cell(row=1, column=1).font = header_font
        try:
            log_sheet.cell(row=len(log_lines) + 3, column=1).fill = header_fill
            log_sheet.cell(row=len(log_lines) + 3, column=1).font = header_font
        except:
            pass
        
        # Create compact comparison of player bets by team - maintaining same order as team configuration
        team_a_bet_comparison = []
        team_b_bet_comparison = []
        
        # Sort team members by stake amount (same as in team configuration)
        team_a_with_stakes = [(p.split(" (")[0], int(p.split("(")[1].split(" ")[0])) for p in result['team_a']]
        team_a_with_stakes.sort(key=lambda x: x[1], reverse=True)
        team_a_sorted_players = [p for p, s in team_a_with_stakes]
        
        team_b_with_stakes = [(p.split(" (")[0], int(p.split("(")[1].split(" ")[0])) for p in result['team_b']]
        team_b_with_stakes.sort(key=lambda x: x[1], reverse=True)
        team_b_sorted_players = [p for p, s in team_b_with_stakes]
        
        # Generate comparison string in the same order as team configuration
        for player_id, max_stake in team_a_with_stakes:
            opt_bet = result['optimized_player_bets'].get(player_id, 0)
            orig_bet = result['original_player_bets'].get(player_id, 0)
            team_a_bet_comparison.append(f"{player_id} ({max_stake}: {orig_bet} / {opt_bet})")
            
        for player_id, max_stake in team_b_with_stakes:
            opt_bet = result['optimized_player_bets'].get(player_id, 0)
            orig_bet = result['original_player_bets'].get(player_id, 0)
            team_b_bet_comparison.append(f"{player_id} ({max_stake}: {orig_bet} / {opt_bet})")
            
        bet_comparison = f"Team A: {', '.join(team_a_bet_comparison)}\nTeam B: {', '.join(team_b_bet_comparison)}"
        
        # Add to comparison sheet
        team_config = f"A: {', '.join(team_a_sorted)}\nB: {', '.join(team_b_sorted)}"
        comparison_sheet.append([
            result['simulation'],
            team_config,
            bet_comparison,
            "\n".join(result['optimized_stake_pairs']),
            result['optimized_total_stake'],
            "\n".join(result['original_stake_pairs']),
            result['original_total_stake']
        ])
        
        # Add to summary
        stake_diff = result['optimized_total_stake'] - result['original_total_stake']
        txn_diff = result['original_num_pairs'] - result['optimized_num_pairs']
        summary_sheet.append([
            result['simulation'],
            result['optimized_total_stake'],
            result['original_total_stake'],
            stake_diff,
            result['optimized_num_pairs'],
            result['original_num_pairs'],
            txn_diff,
            ", ".join(team_a_sorted),
            ", ".join(team_b_sorted)
        ])
    
    # Format comparison sheet
    for row in comparison_sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    
    comparison_sheet.column_dimensions['A'].width = 10
    comparison_sheet.column_dimensions['B'].width = 25
    comparison_sheet.column_dimensions['C'].width = 40
    comparison_sheet.column_dimensions['D'].width = 25
    comparison_sheet.column_dimensions['E'].width = 15
    comparison_sheet.column_dimensions['F'].width = 25
    comparison_sheet.column_dimensions['G'].width = 15
    
    # Format summary sheet conditional formatting
    for row_idx in range(2, summary_sheet.max_row + 1):
        # Color stake difference
        diff_cell = summary_sheet.cell(row=row_idx, column=4)
        if diff_cell.value > 0:
            diff_cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        elif diff_cell.value < 0:
            diff_cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        
        # Color transaction difference
        txn_cell = summary_sheet.cell(row=row_idx, column=7)
        if txn_cell.value > 0:
            txn_cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        elif txn_cell.value < 0:
            txn_cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    
    # Adjust column widths
    for sheet in wb.worksheets:
        for column in sheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = (max_length + 2) if max_length < 80 else 80
            sheet.column_dimensions[column_letter].width = adjusted_width
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"stake_simulations_{timestamp}.xlsx"
    
    # Save workbook
    wb.save(filename)
    print(f"Results saved to {filename}")
    return filename


def main():
    print("Welcome to the Stake Algorithm Tester")
    print("=====================================")
    
    # Get player stakes
    players = get_player_stakes()
    if not players:
        return
    
    # Get min stake
    try:
        min_stake = int(input("Enter minimum stake (default 10): ") or "10")
    except ValueError:
        min_stake = 10
        print("Invalid input. Using default minimum stake of 10.")
    
    # Get multiple
    try:
        multiple = int(input("Enter rounding multiple (default 10): ") or "10")
    except ValueError:
        multiple = 10
        print("Invalid input. Using default multiple of 10.")
    
    # Run simulations
    results, optimized_logs, original_logs = run_stake_simulations(players, min_stake, multiple)
    
    # Write to Excel
    filename = write_to_excel(players, results, optimized_logs, original_logs)
    
    print(f"Testing complete! Results saved to {filename}")


if __name__ == "__main__":
    main()