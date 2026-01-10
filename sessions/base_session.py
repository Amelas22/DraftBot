from database.message_management import make_message_sticky
from models.session_details import SessionDetails
from session import DraftSession, AsyncSessionLocal
from datetime import datetime, timedelta
from helpers.utils import get_cube_thumbnail_url
from views import PersistentView
import discord
from services.draft_setup_manager import DraftSetupManager
import asyncio
from config import get_session_deletion_hours

class BaseSession:
    def __init__(self, session_details: SessionDetails, session_factory=None):
        self.session_details = session_details
        self.draft_manager = None
        self.connection_task = None
        # Use injected session factory or default to production factory
        if session_factory is None:
            from database.db_session import get_session_factory
            session_factory = get_session_factory()
        self.session_factory = session_factory

    async def create_draft_session(self, interaction, bot):
        async with self.session_factory() as session:
            async with session.begin():
                # Step 1: Set up the draft session
                new_draft_session = self.setup_draft_session(session)
                await session.commit()
                
                # Step 2: Set up draft manager and start connection
                self.draft_manager = DraftSetupManager(
                    session_id=new_draft_session.session_id,
                    draft_id=new_draft_session.draft_id,
                    cube_id=new_draft_session.cube
                )
                # Start the connection manager as a background task
                self.connection_task = asyncio.create_task(self.draft_manager.keep_connection_alive())
                
                # Step 3: Create Embed and Persistent View
                embed = self.create_embed()
                view = PersistentView(
                    bot=bot,
                    draft_session_id=new_draft_session.session_id,
                    session_type=self.get_session_type(),
                    team_a_name=new_draft_session.team_a_name,
                    team_b_name=new_draft_session.team_b_name
                )
                await interaction.response.send_message(embed=embed, view=view)

                # Step 4: Get the original response message to set as sticky
                message = await interaction.original_response()

                # Step 5: Update the draft session with message information
                await self.update_message_info(new_draft_session, message)

                # Step 6: Make the message sticky
                await make_message_sticky(interaction.guild.id, message.channel.id, message, view)

    async def cleanup(self):
        """Call this method when the draft session needs to be cleaned up"""
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass

        if self.draft_manager and self.draft_manager.socket_client.connected:
            await self.draft_manager.socket_client.disconnect()

    def setup_draft_session(self, session):
        # Set deletion time based on guild configuration
        session_deletion_hours = get_session_deletion_hours(self.session_details.guild_id)
        deletion_time = datetime.now() + timedelta(hours=session_deletion_hours)
            
        new_draft_session = DraftSession(
            session_id=self.session_details.session_id,
            guild_id=str(self.session_details.guild_id),
            draft_link=self.session_details.draft_link,
            draft_id=self.session_details.draft_id,
            draft_start_time=datetime.fromtimestamp(self.session_details.draft_start_time),
            deletion_time=deletion_time,
            session_type=self.get_session_type(),
            premade_match_id=self.get_premade_match_id(),
            team_a_name=self.session_details.team_a_name,
            team_b_name=self.session_details.team_b_name,
            tracked_draft=True,
            cube=self.session_details.cube_choice,
            min_stake=getattr(self.session_details, 'min_stake', 10)
        )
        session.add(new_draft_session)
        return new_draft_session

    def create_embed(self):
        """Create the base embed that all sessions will extend."""
        # This will be implemented by subclasses
        embed = self._create_embed_content()


        # Add a dedicated Cube field (easier to update in views.py)
        cube_field_value = f"[{self.session_details.cube_choice}](https://cubecobra.com/cube/list/{self.session_details.cube_choice})"
        embed.add_field(name="Cube:", value=cube_field_value, inline=True)

        # Add signup fields (can be overridden by subclasses)
        self._add_signup_fields(embed)

        # Add thumbnail
        embed.set_thumbnail(url=get_cube_thumbnail_url(self.session_details.cube_choice))

        return embed

    def _create_embed_content(self):
        """Implemented in subclasses to provide session-specific embed content."""
        raise NotImplementedError

    def _add_signup_fields(self, embed):
        """Add signup-related fields to the embed. Override in subclasses for custom behavior."""
        embed.add_field(name="Sign-Ups:", value="No players yet.", inline=False)

    def get_session_type(self):
        """Implemented in subclasses."""
        raise NotImplementedError

    def get_premade_match_id(self):
        """Return None by default, overridden in subclasses if applicable."""
        return None

    async def update_message_info(self, draft_session, message):
        async with self.session_factory() as session:
            async with session.begin():
                draft_session = await session.get(DraftSession, draft_session.id)  # Refetch session
                draft_session.message_id = str(message.id)
                draft_session.draft_channel_id = str(message.channel.id)
                await session.commit()

    def get_common_description(self):
        """Generate common description parts for draftmancer link."""
        return (
            "\n\n"  # Add some spacing for formatting
            # Note: Cube information is now in a separate field
            # Each user will get their own personalized link when they sign up
            "**Draftmancer Link**: Click your username below to open your personalized link."
        )