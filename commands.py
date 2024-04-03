from session import register_team_to_db

async def register_team_command(bot):
    @bot.slash_command(name="registerteam", description="Register a new team in the league")
    async def register_team(ctx, team_name: str):
        response = await register_team_to_db(team_name)
        await ctx.respond(response)