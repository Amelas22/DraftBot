import discord
from datetime import datetime
import secrets
from discord.ext import commands
from draft_session import DraftSession
from views import PersistentView
from sessions import add_session


def setup_commands(bot: commands.Bot):
    @bot.slash_command(name='startdraft', description="Start a draft with random teams! Do not use for premade teams.")
    async def start_draft(interaction: discord.Interaction):
        await interaction.response.defer()

        draft_start_time = datetime.now().timestamp()
        session_id = f"{interaction.user.id}-{int(draft_start_time)}"
        draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

        session = DraftSession(session_id, bot=bot)
        session.guild_id = interaction.guild_id
        session.draft_link = draft_link
        session.draft_id = draft_id
        session.draft_start_time = draft_start_time

        add_session(session_id, session)

        cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
        ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} Vintage Cube Draft Queue Open!"
        await interaction.followup.send(ping_message, ephemeral=False)

        embed = discord.Embed(
            title=f"MTGO Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
            description="\n**How to use bot**:\n1. Click sign up and click the draftmancer link. Draftmancer host still has to update settings and  from CubeCobra.\n" +
                            "2. When enough people join (6 or 8), Push Ready Check. Once everyone is ready, push Create Teams\n" +
                            "3. Create Teams will create randoms teams and a corresponding seating order. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                            "4. After the draft, come back to this message (it'll be in pins) and click Create Chat Rooms. After 5 seconds chat rooms will be ready and you can press Post Pairings. This takes 10 seconds to process.\n" +
                            "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                            "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                            f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
            color=discord.Color.dark_magenta()
        )
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")


        view = PersistentView(session_id)
    
        message = await interaction.followup.send(embed=embed, view=view)
        print(f"Session {session_id} has been created.")
        session.draft_message_id = message.id
        session.message_id = message.id
        # Pin the message to the channel
        await message.pin()
    
    @bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(interaction: discord.Interaction):
        await interaction.response.defer()

        draft_start_time = datetime.now().timestamp()
        session_id = f"{interaction.user.id}-{int(draft_start_time)}"
        draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

        session = DraftSession(session_id)
        session.guild_id = interaction.guild_id
        session.draft_link = draft_link
        session.draft_id = draft_id
        session.draft_start_time = draft_start_time
        session.session_type = "premade"

        add_session(session_id, session)

        embed = discord.Embed(
            title=f"MTGO Premade Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
            description="\n**How to use bot**:\n1. Click Team A or Team B to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and  from CubeCobra.\n" +
                            "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
                            "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                            "4. After the draft, come back to this message (it'll be in pins) and click Create Chat Rooms. After 5 seconds chat rooms will be ready and you can press Post Pairings. This takes 10 seconds to process.\n" +
                            "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                            "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                            f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
            color=discord.Color.blue()
        )
        embed.add_field(name="Team A", value="No players yet.", inline=False)
        embed.add_field(name="Team B", value="No players yet.", inline=False)
        embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1219018393471025242/1219410709440495746/image.png?ex=660b33b8&is=65f8beb8&hm=b7e40e9b872d8e04dd70a30c5abc15917379f9acb7dce74ca0372105ec98b468&")

        view = PersistentView(session_id)
    
        message = await interaction.followup.send(embed=embed, view=view)
        print(f"Premade Draft: {session_id} has been created.")
        session.draft_message_id = message.id
        session.message_id = message.id