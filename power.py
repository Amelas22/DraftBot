import asyncio
import json
import csv
from collections import Counter
from sqlalchemy import select, or_, and_
from models import DraftSession, PlayerStats
from database.db_session import db_session

# Define the power cards to check for
POWER_CARDS = [
    "Gleemox",
    "Black Lotus",
    "Ancestral Recall",
    "Mana Crypt",
    "Sol Ring",
    "Time Walk",
    "Mox Jet",  
    "Mox Ruby",
    "Mox Sapphire",
    "Mox Pearl",
    "Mox Emerald"
]

# The guild ID to filter for
TARGET_GUILD_ID = "1355718878298116096"

async def get_matching_drafts():
    """
    Query the database for drafts that meet our criteria:
    - From the specified guild
    - Have a victory message
    - Have pack_first_picks data
    """
    async with db_session() as session:
        query = select(DraftSession).where(
            and_(
                DraftSession.guild_id == TARGET_GUILD_ID,
                or_(
                    DraftSession.victory_message_id_draft_chat.isnot(None),
                    DraftSession.victory_message_id_results_channel.isnot(None)
                ),
                DraftSession.pack_first_picks.isnot(None)
            )
        )
        
        result = await session.execute(query)
        drafts = result.scalars().all()
        return drafts

def analyze_pack_first_picks(pack_first_picks):
    """
    Analyze the pack_first_picks data to identify who picked power cards at any position.
    
    Expected format:
    {
      "player_id1": {
        "0": "Card1",  # First pick
        "1": "Card2",  # Second pick
        "2": "Card3"   # Third pick
      },
      "player_id2": {
        "0": "Card4",
        "1": "Card5",
        "2": "Card6"
      },
      ...
    }
    
    Returns:
    - player_power_cards: Dict mapping player_id to list of power cards picked
    - power_card_count: Counter of power cards found across all picks
    """
    player_power_cards = {}
    power_card_count = Counter()
    
    # Check if pack_first_picks is a string (JSON) and parse it if needed
    if isinstance(pack_first_picks, str):
        try:
            pack_first_picks = json.loads(pack_first_picks)
        except json.JSONDecodeError:
            print(f"Error decoding JSON in pack_first_picks: {pack_first_picks[:100]}...")
            return player_power_cards, power_card_count
    
    if not pack_first_picks:
        return player_power_cards, power_card_count
    
    try:
        # Process the pack_first_picks data in the expected format
        for player_id, picks in pack_first_picks.items():
            # Skip if not a dictionary of picks
            if not isinstance(picks, dict):
                continue
                
            # Check all picks (index "0", "1", "2", etc.)
            for pick_num, card_name in picks.items():
                if not isinstance(card_name, str):
                    continue
                
                # Check if the card is a power card
                if card_name in POWER_CARDS:
                    if player_id not in player_power_cards:
                        player_power_cards[player_id] = []
                    player_power_cards[player_id].append(card_name)
                    power_card_count[card_name] += 1
    except Exception as e:
        print(f"Error processing pack_first_picks: {e}")
        # If there's an error with the expected format, fall back to recursive approach
        return recursive_analyze_pack_first_picks(pack_first_picks)
    
    return player_power_cards, power_card_count

def recursive_analyze_pack_first_picks(pack_first_picks):
    """Fallback method using recursive approach for unexpected data structures"""
    player_power_cards = {}
    power_card_count = Counter()
    
    def process_data(data):
        """
        Recursively process the data structure to find all power card picks.
        Counts cards at any index for each player.
        """
        if isinstance(data, dict):
            for player_id, picks in data.items():
                if isinstance(picks, dict):
                    for pick_num, card_name in picks.items():
                        if isinstance(card_name, str) and card_name in POWER_CARDS:
                            if player_id not in player_power_cards:
                                player_power_cards[player_id] = []
                            player_power_cards[player_id].append(card_name)
                            power_card_count[card_name] += 1
                elif isinstance(picks, dict):
                    # Recursively process nested dictionaries
                    process_data(picks)
    
    # Start processing from the root
    process_data(pack_first_picks)
    
    return player_power_cards, power_card_count

async def get_player_display_names():
    """
    Get a mapping of player_ids to display_names from the PlayerStats table
    """
    from sqlalchemy import select
    from models import PlayerStats
    
    player_names = {}
    async with db_session() as session:
        # Get all player stats records
        query = select(PlayerStats)
        result = await session.execute(query)
        player_stats = result.scalars().all()
        
        # Create a mapping of player_id to display_name
        for player in player_stats:
            if player.player_id and player.display_name:
                player_names[player.player_id] = player.display_name
    
    return player_names

async def main():
    print("Starting analysis of draft data...")
    
    # Get all drafts matching our criteria
    drafts = await get_matching_drafts()
    print(f"Found {len(drafts)} matching drafts")
    
    # Get player display names
    player_names = await get_player_display_names()
    print(f"Found {len(player_names)} player display names")
    
    # Dictionaries and counters to track our statistics
    total_power_card_count = Counter()  # Counter for total frequency of each power card
    player_draft_count = {}  # player_id -> total number of drafts
    player_power_drafts = {}  # player_id -> number of drafts with power cards
    player_power_cards = {}  # player_id -> total number of power cards opened
    
    # Process each draft
    for draft in drafts:
        # Get the list of players from sign_ups field
        all_players = []
        
        # Check sign_ups field first (most reliable)
        if draft.sign_ups:
            if isinstance(draft.sign_ups, str):
                try:
                    sign_ups = json.loads(draft.sign_ups)
                    all_players = list(sign_ups.keys())
                except json.JSONDecodeError:
                    pass
            elif isinstance(draft.sign_ups, dict):
                all_players = list(draft.sign_ups.keys())
        
        # Fallback to team_a and team_b if sign_ups is empty
        if not all_players:
            team_a = draft.team_a if isinstance(draft.team_a, list) else json.loads(draft.team_a or '[]')
            team_b = draft.team_b if isinstance(draft.team_b, list) else json.loads(draft.team_b or '[]')
            all_players = team_a + team_b if team_a and team_b else []
        
        # Update draft count for each player
        for player_id in all_players:
            player_id = str(player_id)  # Ensure player_id is a string
            if player_id not in player_draft_count:
                player_draft_count[player_id] = 0
                player_power_drafts[player_id] = 0
                player_power_cards[player_id] = 0
            
            player_draft_count[player_id] += 1
        
        # Analyze power cards in this draft
        if draft.pack_first_picks:
            try:
                draft_player_power, draft_power_count = analyze_pack_first_picks(draft.pack_first_picks)
                
                # Update total power card count
                total_power_card_count.update(draft_power_count)
                
                # Update player statistics
                for player_id, cards in draft_player_power.items():
                    player_id = str(player_id)  # Ensure player_id is a string
                    
                    # Increment number of drafts with power cards
                    if player_id in player_power_drafts:
                        player_power_drafts[player_id] += 1
                    else:
                        player_power_drafts[player_id] = 1
                    
                    # Increment total power cards opened
                    if player_id in player_power_cards:
                        player_power_cards[player_id] += len(cards)
                    else:
                        player_power_cards[player_id] = len(cards)
            except Exception as e:
                print(f"Error analyzing draft {draft.session_id}: {str(e)}")
    
    # Calculate player rankings
    player_rankings = []
    
    for player_id, total_drafts in player_draft_count.items():
        # Skip players with fewer than 5 drafts
        if total_drafts < 15:
            continue
            
        # Skip players with 0-1 power cards
        if player_id not in player_power_cards or player_power_cards[player_id] <= 1:
            continue
        
        drafts_with_power = player_power_drafts.get(player_id, 0)
        total_power = player_power_cards.get(player_id, 0)
        
        # Calculate percentage of drafts with power cards
        power_percentage = (drafts_with_power / total_drafts) * 100 if total_drafts > 0 else 0
        
        # Get player display name
        display_name = player_names.get(player_id, f"Unknown Player ({player_id})")
        
        player_rankings.append({
            'player_id': player_id,
            'display_name': display_name,
            'total_drafts': total_drafts,
            'drafts_with_power': drafts_with_power,
            'total_power_cards': total_power,
            'power_percentage': power_percentage
        })
    
    # Sort by percentage (highest first)
    player_rankings.sort(key=lambda x: x['power_percentage'], reverse=True)
    
    # Print results
    print("\n===== POWER CARD FREQUENCY =====")
    print("How often each power card was opened in drafts:")
    for card, count in total_power_card_count.most_common():
        print(f"{card}: {count} times")
    
    print("\n===== PLAYER RANKINGS =====")
    print("Ranked by percentage of drafts where they opened a power card:")
    for rank, player in enumerate(player_rankings, 1):
        print(f"{rank}. {player['display_name']}: "
              f"{player['drafts_with_power']}/{player['total_drafts']} drafts with power "
              f"({player['power_percentage']:.2f}%) - "
              f"Total power cards: {player['total_power_cards']}")
    
    # Also output a CSV file for further analysis

    with open('power_cards_analysis.csv', 'w', newline='') as csvfile:
        fieldnames = ['rank', 'player_id', 'display_name', 'total_drafts', 
                        'drafts_with_power', 'power_percentage', 'total_power_cards']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        writer.writeheader()
        for rank, player in enumerate(player_rankings, 1):
            writer.writerow({
                'rank': rank,
                'player_id': player['player_id'],
                'display_name': player['display_name'],
                'total_drafts': player['total_drafts'],
                'drafts_with_power': player['drafts_with_power'],
                'power_percentage': player['power_percentage'],
                'total_power_cards': player['total_power_cards']
            })
    print("\nResults also saved to power_cards_analysis.csv")

if __name__ == "__main__":
    asyncio.run(main())