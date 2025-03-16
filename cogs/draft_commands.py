import discord
from discord.ext import commands
from loguru import logger
from modals import CubeDraftSelectionView, StakedCubeDraftSelectionView

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
    async def start_draft(self, ctx):
        logger.info("Received startdraft command")
        view = CubeDraftSelectionView(session_type="random")
        await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)

    @discord.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(self, ctx):
        logger.info("Received premadedraft command")
        view = CubeDraftSelectionView(session_type="premade")
        await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)
        
    @discord.slash_command(name='dynamic_stake', description='Start a team draft with random teams and customizable stakes')
    async def staked_draft(self, ctx):
        logger.info("Received stakedraft command")
        view = StakedCubeDraftSelectionView()
        await ctx.response.send_message("Select a cube for the staked draft:", view=view, ephemeral=True)

def setup(bot):
    bot.add_cog(DraftCommands(bot))
