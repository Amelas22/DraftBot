from datetime import datetime, timedelta
from sqlalchemy import select
import discord
import random
from session import DraftSession, AsyncSessionLocal, get_draft_session
from views import PersistentView

class CubeSelectionModal(discord.ui.Modal):
    def __init__(self, session_type, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_type = session_type
        self.add_item(discord.ui.InputText(label="Cube Name", placeholder="LSVCube, AlphaFrog, mtgovintage, or your choice", custom_id="cube_name_input"))
        if self.session_type == "premade":
            self.add_item(discord.ui.InputText(label="Team A Name", placeholder="Team A Name", custom_id="team_a_input"))
            self.add_item(discord.ui.InputText(label="Team B Name", placeholder="Team B Name", custom_id="team_b_input"))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        cube_name = self.children[0].value
        if self.session_type == "premade":
            team_a_name = self.children[1].value
            team_b_name = self.children[2].value

        cube_option = "MTG" if not cube_name else cube_name
        draft_start_time = datetime.now().timestamp()
        session_id = f"{interaction.user.id}-{int(draft_start_time)}"
        draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

        async with AsyncSessionLocal() as session:
            async with session.begin():
                new_draft_session = DraftSession(
                    session_id=session_id,
                    guild_id=str(interaction.guild_id),
                    draft_link=draft_link,
                    draft_id=draft_id,
                    draft_start_time=datetime.now(),
                    deletion_time=datetime.now() + timedelta(hours=3),
                    session_type=self.session_type,
                    premade_match_id=None if self.session_type != "swiss" else 9000,
                    team_a_name=None if self.session_type != "premade" else team_a_name,
                    team_b_name=None if self.session_type != "premade" else team_b_name,
                    tracked_draft = True,
                    cube = cube_option
                )
                session.add(new_draft_session)

        await interaction.response.send_message("Setting up a draft...")
        if self.session_type == "random":
            cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
            ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} {cube_option} Cube Draft Queue Open!"
            await interaction.followup.send(ping_message)

            # Create the embed with cube_option in the title
            embed_title = f"Looking for Players! {cube_option} Random Team Draft - Queue Opened <t:{int(draft_start_time)}:R>"
            embed = discord.Embed(title=embed_title,
            description="\n**How to use bot**:\n1. Click sign up and click the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                            "2. When enough people join (6 or 8), Push Ready Check. Once everyone is ready, push Create Teams\n" +
                            "3. Create Teams will create randoms teams and a corresponding seating order. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                            "4. After the draft, come back to this message (it'll be in pins) and click Create Rooms and Post Pairings. This will create a shared draft-chat and private team chats. Pairings will post in the draft-chat.\n" +
                            "5. After each match, you can select the Match Results buttons to report results. Once a winner is determined, it will be announced to the channel and to team-draft-results\n" +
                            "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                            f"\n\n**Chosen Cube: [{cube_option}](https://cubecobra.com/cube/list/{cube_option})** \n**Draftmancer Session**: **[Join Here]({draft_link})**",
            color=discord.Color.dark_magenta()
        )
            embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
            # thumbnail by chosen cube?
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")
            print(f"Random Draft: {session_id} has been created.")
            
        elif self.session_type == "swiss":
            embed_title = f"AlphaFrog Prelims: Looking for Players! Queue Opened <t:{int(draft_start_time)}:R>"
            embed = discord.Embed(title=embed_title,
                description="Swiss 8 player draft. Draftmancer host must still update the draftmanacer session with the chosen cube. Turn off randomized seating." +
                f"\n\n**Weekly Cube: [{cube_name}](https://cubecobra.com/cube/list/{cube_name})** \n**Draftmancer Session**: **[Join Here]({draft_link})**",
                color=discord.Color.dark_gold()
                )
            embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
            print(f"Swiss Draft session {session_id} has been created.")
            
        elif self.session_type == "premade":
            team_a_option = "Team A" if not team_a_name else team_a_name
            team_b_option = "Team B" if not team_b_name else team_b_name
            embed = discord.Embed(
                title=f"{cube_option} Premade Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
                description="\n**How to use bot**:\n1. Click Team A or Team B to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                                "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
                                "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                                "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n" +
                                "5. You will now have a private team chat with just your team and a shared draft-chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                                "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                                f"\n\n**Chosen Cube: [{cube_option}](https://cubecobra.com/cube/list/{cube_option})** \n**Draftmancer Session**: **[Join Here]({draft_link})**",
                color=discord.Color.blue()
            )
            embed.add_field(name=f"{team_a_option}", value="No players yet.", inline=False)
            embed.add_field(name=f"{team_b_option}", value="No players yet.", inline=False)
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1219018393471025242/1219410709440495746/image.png?ex=660b33b8&is=65f8beb8&hm=b7e40e9b872d8e04dd70a30c5abc15917379f9acb7dce74ca0372105ec98b468&")
            self.team_a_name = team_a_option
            self.team_b_name = team_b_option

            print(f"Premade Draft: {session_id} has been created.")

        
        draft_session = await get_draft_session(session_id)
        if draft_session:
            view = PersistentView(
                bot=bot,
                draft_session_id=draft_session.session_id,
                session_type=self.session_type,
                team_a_name=getattr(draft_session, 'team_a_name', None),
                team_b_name=getattr(draft_session, 'team_b_name', None)
            )
            message = await interaction.followup.send(embed=embed, view=view)
        
        if new_draft_session:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    result = await session.execute(select(DraftSession).filter_by(session_id=new_draft_session.session_id))
                    updated_session = result.scalars().first()
                    if updated_session:
                        updated_session.message_id = str(message.id)
                        updated_session.draft_channel_id = str(message.channel.id)
                        session.add(updated_session)
                        await session.commit()

        # Pin the message to the channel
        await message.pin()


