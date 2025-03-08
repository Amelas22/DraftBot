from database.message_management import make_message_sticky
from models.session_details import SessionDetails
from session import DraftSession, AsyncSessionLocal
from datetime import datetime, timedelta
from views import PersistentView
import discord

class BaseSession:
    def __init__(self, session_details: SessionDetails):
        self.session_details = session_details

    async def create_draft_session(self, interaction, bot):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Step 1: Set up the draft session
                new_draft_session = self.setup_draft_session(session)
                await session.commit()

                # Step 2: Create Embed and Persistent View
                embed = self.create_embed()
                view = PersistentView(
                    bot=bot,
                    draft_session_id=new_draft_session.session_id,
                    session_type=self.get_session_type(),
                    team_a_name=new_draft_session.team_a_name,
                    team_b_name=new_draft_session.team_b_name
                )
                await interaction.response.send_message(embed=embed, view=view)

                # Step 3: Get the original response message to set as sticky
                message = await interaction.original_response()

                # Step 4: Update the draft session with message information
                await self.update_message_info(new_draft_session, message)

                # Step 5: Make the message sticky (replace pinning with sticky logic)
                await make_message_sticky(interaction.guild.id, message.channel.id, message, view)

    def setup_draft_session(self, session):
        new_draft_session = DraftSession(
            session_id=self.session_details.session_id,
            guild_id=str(self.session_details.guild_id),
            draft_link=self.session_details.draft_link,
            draft_id=self.session_details.draft_id,
            draft_start_time=datetime.fromtimestamp(self.session_details.draft_start_time),
            deletion_time=datetime.fromtimestamp(self.session_details.draft_start_time) + timedelta(hours=3),
            session_type=self.get_session_type(),
            premade_match_id=self.get_premade_match_id(),
            team_a_name=self.session_details.team_a_name,
            team_b_name=self.session_details.team_b_name,
            tracked_draft=True,
            cube=self.session_details.cube_choice
        )
        session.add(new_draft_session)
        return new_draft_session

    def create_embed(self):
        """Implemented in subclasses."""
        raise NotImplementedError

    def get_session_type(self):
        """Implemented in subclasses."""
        raise NotImplementedError

    def get_premade_match_id(self):
        """Return None by default, overridden in subclasses if applicable."""
        return None

    async def update_message_info(self, draft_session, message):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                draft_session = await session.get(DraftSession, draft_session.id)  # Refetch session
                draft_session.message_id = str(message.id)
                draft_session.draft_channel_id = str(message.channel.id)
                await session.commit()

