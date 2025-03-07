def convert_to_american_odds(decimal_odds):
    """
    Convert decimal odds to American odds format.
    
    Examples:
    - 1.5 decimal odds = -200 American odds (favorite)
    - 2.5 decimal odds = +150 American odds (underdog)
    """
    if decimal_odds >= 2.0:
        # Underdog: positive odds showing potential profit on a 100 unit bet
        american_odds = int(round((decimal_odds - 1) * 100))
        return f"+{american_odds}"
    else:
        # Favorite: negative odds showing how much to bet to win 100 units
        american_odds = int(round(-100 / (decimal_odds - 1)))
        return f"{american_odds}"  # Already negative
        
def convert_probability_to_american_odds(probability):
    """Convert a win probability directly to American odds."""
    if probability <= 0:
        return "+1500"  # Maximum odds for extreme underdogs
    if probability >= 1:
        return "-10000"  # Maximum odds for overwhelming favorites
    
    # Basic formula: odds = 1 / probability
    raw_odds = 1 / probability
    
    # Apply house edge (reduce payout slightly)
    margin = 1.05  # 10% margin
    adjusted_odds = raw_odds * margin
    
    # Convert to American format
    return convert_to_american_odds(adjusted_odds)