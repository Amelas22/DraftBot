import discord
from discord.ui import Button, View, Select
import asyncio
from sqlalchemy import select
from datetime import datetime, timedelta
from loguru import logger
from session import AsyncSessionLocal, DraftSession, PlayerStats
from typing import Dict, List

class TeamBetButton(Button):
    def __init__(self, market_id: int, outcome: str, label: str, american_odds: str, decimal_odds: float):
        # Set button style based on outcome
        style = discord.ButtonStyle.danger if outcome == "team_a" else \
                discord.ButtonStyle.success if outcome == "team_b" else \
                discord.ButtonStyle.secondary
                
        super().__init__(style=style, label=f"{label} ({american_odds})", custom_id=f"bet_{market_id}_{outcome}")
        self.market_id = market_id
        self.outcome = outcome
        self.decimal_odds = decimal_odds
        
    async def callback(self, interaction: discord.Interaction):
        # Create dropdown for betting amount
        view = BetAmountView(self.market_id, self.outcome, self.decimal_odds)
        await interaction.response.send_message(
            f"Select bet amount for {self.label}:", 
            view=view, 
            ephemeral=True
        )

class TrophyBetButton(Button):
    def __init__(self, market_id: int, player_id: str, player_name: str, american_odds: str, decimal_odds: float):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"{player_name} ({american_odds})",
            custom_id=f"trophy_{market_id}_{player_id}"
        )
        self.market_id = market_id
        self.player_id = player_id
        self.outcome = "trophy"  # When betting on trophy, the outcome is always "trophy"
        self.decimal_odds = decimal_odds
        
    async def callback(self, interaction: discord.Interaction):
        # Create dropdown for betting amount
        view = BetAmountView(self.market_id, self.outcome, self.decimal_odds)
        await interaction.response.send_message(
            f"Select bet amount for {self.label}:", 
            view=view, 
            ephemeral=True
        )

class BetAmountSelect(Select):
    def __init__(self, market_id: int, outcome: str, decimal_odds: float):
        options = []
        for amount in range(25, 276, 25):  # 25, 50, 75, ..., 250
            # Calculate potential profit (not including the stake)
            profit = int(amount * (decimal_odds - 1))
            options.append(
                discord.SelectOption(
                    label=f"Bet {amount} coins",
                    description=f"Win {profit} coins",
                    value=str(amount)
                )
            )
            
        super().__init__(
            placeholder="Select bet amount...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"amount_{market_id}_{outcome}"
        )
        
        self.market_id = market_id
        self.outcome = outcome
        self.decimal_odds = decimal_odds
        
    async def callback(self, interaction: discord.Interaction):
        # Get selected amount
        amount = int(self.values[0])
        
        # Place bet
        from betting_utilities import place_bet
        
        user_id = str(interaction.user.id)
        display_name = interaction.user.display_name
        
        result = await place_bet(user_id, display_name, self.market_id, amount, self.outcome)
        
        if result["success"]:
            # Calculate profit (not including stake)
            profit = int(amount * (self.decimal_odds - 1))
            
            embed = discord.Embed(
                title="Bet Placed!",
                color=discord.Color.green(),
                description=f"You bet **{amount}** coins with potential profit of **{profit}** coins."
            )
            
            embed.add_field(
                name="New Balance",
                value=f"**{result['new_balance']}** coins",
                inline=False
            )
            
            await interaction.response.edit_message(content=None, embed=embed, view=None)
        else:
            await interaction.response.edit_message(
                content=f"Error placing bet: {result['error']}",
                view=None
            )

class BetAmountView(View):
    def __init__(self, market_id: int, outcome: str, decimal_odds: float):
        super().__init__(timeout=60)  # 1 minute timeout
        self.add_item(BetAmountSelect(market_id, outcome, decimal_odds))

class TeamBettingView(View):
    def __init__(self, markets: Dict, team_a_name: str, team_b_name: str, has_draw: bool = False):
        super().__init__(timeout=15*60)  # 15 minute timeout
        
        # Find team win market
        team_win_market = next((m for m in markets if m.market_type == 'team_win'), None)
        if not team_win_market:
            return
            
        # Convert decimal odds to American
        from american_odds_conversion import convert_to_american_odds
        team_a_american = convert_to_american_odds(team_win_market.team_a_odds)
        team_b_american = convert_to_american_odds(team_win_market.team_b_odds)
        
        # Add buttons for team A and B
        self.add_item(TeamBetButton(
            team_win_market.id, "team_a", team_a_name, 
            team_a_american, team_win_market.team_a_odds
        ))
        self.add_item(TeamBetButton(
            team_win_market.id, "team_b", team_b_name, 
            team_b_american, team_win_market.team_b_odds
        ))
        
        # Add draw button if applicable
        if has_draw and team_win_market.draw_odds:
            draw_american = convert_to_american_odds(team_win_market.draw_odds)
            self.add_item(TeamBetButton(
                team_win_market.id, "draw", "Match Draw", 
                draw_american, team_win_market.draw_odds
            ))

class TrophyBettingView(View):
    def __init__(self, markets: List, player_map: Dict):
        super().__init__(timeout=15*60)  # 15 minute timeout
        
        # Add buttons for each player's trophy market
        from american_odds_conversion import convert_to_american_odds
        
        for market in markets:
            if market.market_type == 'player_trophy' and market.player_id in player_map:
                player_name = player_map[market.player_id]
                american_odds = convert_to_american_odds(market.trophy_odds)
                
                self.add_item(TrophyBetButton(
                    market.id, market.player_id, player_name,
                    american_odds, market.trophy_odds
                ))

async def get_player_trueskill_ratings(player_ids):
    """Get TrueSkill ratings for a list of players."""
    player_ratings = {}
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for player_id in player_ids:
                query = select(PlayerStats).where(PlayerStats.player_id == player_id)
                result = await session.execute(query)
                player_stats = result.scalar_one_or_none()
                
                if player_stats:
                    player_ratings[player_id] = {
                        "mu": player_stats.true_skill_mu,
                        "sigma": player_stats.true_skill_sigma
                    }
                else:
                    # Default values for new players
                    player_ratings[player_id] = {
                        "mu": 25.0,
                        "sigma": 8.333
                    }
                    
    return player_ratings

async def manage_betting_period(bot, draft_session_id, channel_id):
    """Create and manage the betting period for a draft."""
    from session import BettingMarket
    
    try:
        # Get draft session
        async with AsyncSessionLocal() as session:
            async with session.begin():
                query = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                result = await session.execute(query)
                draft_session = result.scalar_one_or_none()
                
                if not draft_session:
                    logger.error(f"Draft session {draft_session_id} not found for betting")
                    return
                
                # Get betting markets
                markets_query = select(BettingMarket).where(
                    BettingMarket.draft_session_id == draft_session_id,
                    BettingMarket.status == 'open'
                )
                markets_result = await session.execute(markets_query)
                markets = markets_result.scalars().all()
                
                if not markets:
                    logger.error(f"No betting markets found for draft {draft_session_id}")
                    return
        
        # Get channel
        channel = bot.get_channel(int(channel_id))
        if not channel:
            logger.error(f"Channel {channel_id} not found")
            return
        
        # Get TrueSkill ratings for all players
        all_player_ids = draft_session.team_a + draft_session.team_b
        player_ratings = await get_player_trueskill_ratings(all_player_ids)
        
        # Create player ID to name mapping with TrueSkill ratings
        player_map = {}
        team_a_display = []
        team_b_display = []
        
        for player_id in draft_session.team_a:
            display_name = draft_session.sign_ups.get(player_id, f"Player {player_id}")
            player_map[player_id] = display_name
            
            # Add TrueSkill rating to the display
            mu = player_ratings[player_id]["mu"]
            team_a_display.append(f"{display_name} [Rating: {mu:.1f}]")
            
        for player_id in draft_session.team_b:
            display_name = draft_session.sign_ups.get(player_id, f"Player {player_id}")
            player_map[player_id] = display_name
            
            # Add TrueSkill rating to the display
            mu = player_ratings[player_id]["mu"]
            team_b_display.append(f"{display_name} [Rating: {mu:.1f}]")
        
        # Create team names
        team_a_name = draft_session.team_a_name or "Team A"
        team_b_name = draft_session.team_b_name or "Team B"
        
        # Create betting embeds
        has_draw = len(draft_session.team_a) == 4 and len(draft_session.team_b) == 4
        
        # Calculate betting close time (15 minutes from now)
        close_time = datetime.now() + timedelta(minutes=15)
        close_timestamp = int(close_time.timestamp())
        
        team_embed = discord.Embed(
            title="📢 PLACE YOUR BETS - Team Winner",
            description=f"Betting closes <t:{close_timestamp}:R>. Use the buttons below to bet on the match winner.",
            color=discord.Color.gold()
        )
        
        # Add team fields with TrueSkill ratings
        team_embed.add_field(
            name=f"{team_a_name}",
            value="\n".join(team_a_display),
            inline=True
        )
        team_embed.add_field(
            name=f"{team_b_name}",
            value="\n".join(team_b_display),
            inline=True
        )
        
        team_embed.set_footer(text=f"Claim daily coins with /claim | Check balance with /balance")
        
        trophy_embed = discord.Embed(
            title="🏆 PLACE YOUR BETS - Trophy Winners",
            description="Bet on which players will go 3-0 and earn a trophy!",
            color=discord.Color.purple()
        )
        trophy_embed.add_field(
            name="Trophy Bets",
            value="Click a player's button below to bet on them getting a trophy. All bets are refunded if no result is determined within 8 hours.",
            inline=False
        )
        trophy_embed.set_footer(text=f"Claim daily coins with /claim | Check balance with /balance")
        
        # Create views
        team_view = TeamBettingView(markets, team_a_name, team_b_name, has_draw)
        trophy_view = TrophyBettingView(markets, player_map)
        
        # Send embeds
        team_message = await channel.send(embed=team_embed, view=team_view)
        trophy_message = await channel.send(embed=trophy_embed, view=trophy_view)
        
        # Store message IDs
        async with AsyncSessionLocal() as session:
            async with session.begin():
                draft_query = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                draft_result = await session.execute(draft_query)
                draft_to_update = draft_result.scalar_one_or_none()
                
                if draft_to_update:
                    draft_to_update.betting_team_message_id = str(team_message.id)
                    draft_to_update.betting_trophy_message_id = str(trophy_message.id)
                    draft_to_update.betting_close_time = close_time
                    await session.commit()
        
        # Wait for 15 minutes
        await asyncio.sleep(15 * 60)
        
        # Create summary before deleting messages
        summary_embed = await create_betting_summary(draft_session_id)
        
        # Delete betting messages
        try:
            await team_message.delete()
            await trophy_message.delete()
        except Exception as e:
            logger.error(f"Failed to delete betting messages: {e}")
        
        # Post summary
        if summary_embed:
            await channel.send(embed=summary_embed)
        
        # Close betting markets
        await close_betting_markets(draft_session_id)
        
        # Schedule a check to refund bets if no result after 8 hours
        asyncio.create_task(
            check_and_refund_bets_if_needed(draft_session_id, bot, channel_id)
        )
        
    except Exception as e:
        logger.error(f"Error in manage_betting_period: {e}")

async def check_and_refund_bets_if_needed(draft_session_id, bot, channel_id, hours=8):
    """Check if the draft has a result after specified hours and refund bets if not."""
    await asyncio.sleep(hours * 60 * 60)  # Wait for specified hours
    
    try:
        # Check if the draft has a result
        async with AsyncSessionLocal() as session:
            async with session.begin():
                query = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                result = await session.execute(query)
                draft_session = result.scalar_one_or_none()
                
                if not draft_session:
                    logger.error(f"Draft session {draft_session_id} not found for refund check")
                    return
                
                # If there's no victory message, refund all bets
                if not draft_session.victory_message_id_draft_chat:
                    # Refund all bets
                    from betting_utilities import refund_all_bets
                    refund_result = await refund_all_bets(draft_session_id)
                    
                    # Send notification to the channel
                    channel = bot.get_channel(int(channel_id))
                    if channel:
                        embed = discord.Embed(
                            title="⚠️ Betting Refunds Issued",
                            description=f"The draft didn't complete within {hours} hours. All bets have been refunded.",
                            color=discord.Color.orange()
                        )
                        embed.add_field(
                            name="Refund Details",
                            value=f"Refunded {refund_result['bet_count']} bets totaling {refund_result['total_amount']} coins.",
                            inline=False
                        )
                        await channel.send(embed=embed)
    
    except Exception as e:
        logger.error(f"Error in check_and_refund_bets_if_needed: {e}")

async def create_betting_summary(draft_session_id):
    """Create a summary of all bets placed on a draft."""
    from session import BettingMarket, UserBet
    
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get draft session
                draft_query = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                draft_result = await session.execute(draft_query)
                draft_session = draft_result.scalar_one_or_none()
                
                if not draft_session:
                    return None
                
                # Get markets for this draft
                markets_query = select(BettingMarket).where(BettingMarket.draft_session_id == draft_session_id)
                markets_result = await session.execute(markets_query)
                markets = {market.id: market for market in markets_result.scalars().all()}
                
                if not markets:
                    return None
                
                # Get all bets for these markets
                bets_query = select(UserBet).where(
                    UserBet.market_id.in_(list(markets.keys())),
                    UserBet.status == 'active'
                )
                bets_result = await session.execute(bets_query)
                bets = bets_result.scalars().all()
                
                if not bets:
                    return discord.Embed(
                        title="No Bets Placed",
                        description="No bets were placed during the betting period.",
                        color=discord.Color.light_grey()
                    )
        
        # Group bets by market type and outcome
        team_bets = {}
        trophy_bets = {}
        
        for bet in bets:
            market = markets.get(bet.market_id)
            if not market:
                continue
            
            if market.market_type == 'team_win':
                if bet.selected_outcome not in team_bets:
                    team_bets[bet.selected_outcome] = []
                team_bets[bet.selected_outcome].append(bet)
            elif market.market_type == 'player_trophy':
                if bet.selected_outcome not in trophy_bets:
                    trophy_bets[bet.selected_outcome] = []
                trophy_bets[bet.selected_outcome].append(bet)
        
        # Create summary embed
        embed = discord.Embed(
            title="📊 Betting Summary",
            description=f"All bets placed for Draft #{draft_session.draft_id}",
            color=discord.Color.gold()
        )
        
        # Add team bets summary
        if team_bets:
            team_a_name = draft_session.team_a_name or "Team A"
            team_b_name = draft_session.team_b_name or "Team B"
            
            team_field = ""
            
            # Team A bets
            team_a_bets = team_bets.get('team_a', [])
            if team_a_bets:
                team_field += f"**{team_a_name} Win:**\n"
                for bet in sorted(team_a_bets, key=lambda x: x.bet_amount, reverse=True):
                    team_field += f"{bet.display_name}: {bet.bet_amount} coins\n"
                team_field += "\n"
            
            # Team B bets
            team_b_bets = team_bets.get('team_b', [])
            if team_b_bets:
                team_field += f"**{team_b_name} Win:**\n"
                for bet in sorted(team_b_bets, key=lambda x: x.bet_amount, reverse=True):
                    team_field += f"{bet.display_name}: {bet.bet_amount} coins\n"
                team_field += "\n"
            
            # Draw bets
            draw_bets = team_bets.get('draw', [])
            if draw_bets:
                team_field += f"**Match Draw:**\n"
                for bet in sorted(draw_bets, key=lambda x: x.bet_amount, reverse=True):
                    team_field += f"{bet.display_name}: {bet.bet_amount} coins\n"
            
            if team_field:
                embed.add_field(
                    name="Team Winner Bets",
                    value=team_field,
                    inline=False
                )
        
        # Add trophy bets summary
        if trophy_bets:
            trophy_field = ""
            
            # Trophy bets
            trophy_players = trophy_bets.get('trophy', [])
            if trophy_players:
                # Group by player
                player_bets = {}
                for bet in trophy_players:
                    market = markets.get(bet.market_id)
                    if not market or not market.player_id:
                        continue
                        
                    player_id = market.player_id
                    player_name = draft_session.sign_ups.get(player_id, f"Player {player_id}")
                    
                    if player_name not in player_bets:
                        player_bets[player_name] = []
                    player_bets[player_name].append(bet)
                
                # Format each player's bets
                for player_name, bets in sorted(player_bets.items()):
                    trophy_field += f"**{player_name} Trophy:**\n"
                    for bet in sorted(bets, key=lambda x: x.bet_amount, reverse=True):
                        trophy_field += f"{bet.display_name}: {bet.bet_amount} coins\n"
                    trophy_field += "\n"
            
            if trophy_field:
                embed.add_field(
                    name="Trophy Bets",
                    value=trophy_field,
                    inline=False
                )
        
        # Add total bet amount
        total_amount = sum(bet.bet_amount for bet in bets)
        embed.set_footer(text=f"Total bets: {len(bets)} | Total amount: {total_amount} coins")
        
        return embed
        
    except Exception as e:
        logger.error(f"Error creating betting summary: {e}")
        return None

async def close_betting_markets(draft_session_id):
    """Close all open betting markets for a draft."""
    from session import BettingMarket
    
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get open markets for this draft
                markets_query = select(BettingMarket).where(
                    BettingMarket.draft_session_id == draft_session_id,
                    BettingMarket.status == 'open'
                )
                markets_result = await session.execute(markets_query)
                markets = markets_result.scalars().all()
                
                # Update market status
                for market in markets:
                    market.status = 'closed'
                
                await session.commit()
                
    except Exception as e:
        logger.error(f"Error closing betting markets: {e}")