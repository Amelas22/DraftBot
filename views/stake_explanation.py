"""
Stake explanation embeds for the draft bot.
"""

import discord


def create_stake_explanation_embed() -> discord.Embed:
    """Create an embed explaining how the stake system works."""
    embed = discord.Embed(
        title="How the Dynamic Bet System Works",
        description=(
            "The dynamic bet system allows players to bet different amounts based on their personal preferences "
            "to ensure all players can bet what they are comfortable with."
        ),
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="Core Principles",
        value=(
            "• **Max Bet Protection**: You will never be allocated more than your maximum bet amount\n"
            "• **Team Formation**: Teams are created randomly FIRST, then bets are allocated\n"
            "• **Flexibility**: The system adapts to different betting situations using two methods"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Process Overview",
        value=(
            "The betting process works in two phases:\n"
            "1. **Allocation Phase**: Determine how much each player will bet\n"
            "2. **Bet Matching Phase**: Create player-to-player betting pairs"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Bet Capping Option",
        value=(
            "• Players can choose \"capped\" (🧢) or \"uncapped\" (🏎️)\n"
            "• Capped bets are limited to the highest bet on the opposing team\n"
            "• This is applied before any calculations occur"
        ),
        inline=False
    )
    
    # Add more fields as needed...
    
    return embed