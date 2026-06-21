import asyncio

import discord
from loguru import logger
from typing import Dict, Iterable, List, Literal, Optional

from helpers.display_names import get_display_name_by_id
from models import SignUpHistory
from services.state_manager import state_manager
from services.team_creator import create_and_display_teams
from session import get_draft_session

PlayerStatus = Literal['ready', 'not_ready', 'no_response']

# After this long with no response and no re-fire, a stalled lobby ready check is
# auto-cleaned and a notice is posted. Must exceed the re-fire debounce in views.py
# so a host can re-trigger (which cleans up silently) before this fires.
READY_CHECK_TIMEOUT_SECONDS = 300


class ReadyCheckSession:
    """In-memory state, business logic, and coordination for a single active in-chat ready check."""

    def __init__(self, player_ids: Iterable[str], message_id: Optional[int] = None):
        self.message_id: Optional[int] = message_id
        # Single source of truth: player_id -> status
        self._players: Dict[str, PlayerStatus] = {pid: 'no_response' for pid in player_ids}

    # --- player state ---

    def set_status(self, player_id: str, status: PlayerStatus) -> None:
        """Update a tracked player's status. No-op if not in the check."""
        if player_id in self._players:
            self._players[player_id] = status

    def add_player(self, player_id: str) -> None:
        """Add a newly joined player as no_response. No-op if already tracked."""
        if player_id not in self._players:
            self._players[player_id] = 'no_response'

    def remove_player(self, player_id: str) -> None:
        """Remove a player who left the draft. No-op if not present."""
        self._players.pop(player_id, None)

    def has_player(self, player_id: str) -> bool:
        return player_id in self._players

    def players_with_status(self, status: PlayerStatus) -> List[str]:
        return [pid for pid, s in self._players.items() if s == status]

    def all_ready(self) -> bool:
        """True when every player has responded ready and at least one player exists."""
        return (
            len(self._players) > 0
            and all(s == 'ready' for s in self._players.values())
        )

    def counts(self) -> Dict[str, int]:
        """Return the number of players in each ready-check bucket (for logging)."""
        return {
            "ready": len(self.ready),
            "not_ready": len(self.not_ready),
            "no_response": len(self.no_response),
        }

    @property
    def ready(self) -> List[str]:
        return self.players_with_status('ready')

    @property
    def not_ready(self) -> List[str]:
        return self.players_with_status('not_ready')

    @property
    def no_response(self) -> List[str]:
        return self.players_with_status('no_response')

    # --- Discord message operations (instance) ---

    async def build_embed(self, sign_ups: dict, guild=None) -> discord.Embed:
        """Build a ready check embed from current player state."""
        def get_names(user_ids):
            names = []
            for uid in user_ids:
                if guild:
                    name = get_display_name_by_id(uid, guild, sign_ups.get(uid, "Unknown user"))
                else:
                    name = sign_ups.get(uid, "Unknown user")
                names.append(name)
            return "\n".join(names) or "None"

        embed = discord.Embed(
            title="Ready Check Initiated",
            description="Please indicate if you are ready.\n\nDraftmancer links will be provided once teams are created.",
            color=discord.Color.gold()
        )
        embed.add_field(name="Ready", value=get_names(self.ready), inline=False)
        embed.add_field(name="Not Ready", value=get_names(self.not_ready), inline=False)
        embed.add_field(name="No Response", value=get_names(self.no_response), inline=False)
        return embed

    async def delete_message(self, channel) -> None:
        """Delete the Discord ready check message. No-op if message is missing or already gone."""
        if not (self.message_id and channel):
            return
        try:
            msg = await channel.fetch_message(self.message_id)
            await msg.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error(f"Error deleting ready check message {self.message_id}: {e}")

    async def refresh_embed(self, channel, sign_ups: dict, guild=None, title: Optional[str] = None) -> bool:
        """Rebuild the embed and edit the Discord message in-place.
        Returns True on success. On NotFound, clears message_id and returns False.
        """
        if not self.message_id:
            return False
        try:
            embed = await self.build_embed(sign_ups, guild=guild)
            if title:
                embed.title = title
            msg = await channel.fetch_message(self.message_id)
            await msg.edit(embed=embed)
            return True
        except discord.NotFound:
            self.message_id = None
            return False
        except Exception as e:
            logger.error(f"Error refreshing ready check embed: {e}")
            return False

    async def run_timeout(self, session_id: str, channel, guild) -> None:
        """After READY_CHECK_TIMEOUT_SECONDS, if THIS check is still the active one
        and stalled (some players never responded), audit the non-responders, delete
        the stale message, drop the state, and post a notice.

        No-op if the check completed, everyone responded, or it was superseded by a
        re-fire (the re-fire cleans up the old message silently, so no notice here).
        """
        await asyncio.sleep(READY_CHECK_TIMEOUT_SECONDS)

        # Superseded by a re-fire or already torn down -> the other path handled it.
        if state_manager.get_ready_check(session_id) is not self:
            return
        # Completed (handle_all_ready in flight) -> leave it alone.
        if self.all_ready():
            return

        non_responders = self.no_response
        if not non_responders:
            # Everyone responded (some Not Ready); not a silent stall, nothing to flag.
            return

        guild_id = str(guild.id) if guild else "unknown"
        draft_session = await get_draft_session(session_id)
        sign_ups = (draft_session.sign_ups or {}) if draft_session else {}

        logger.warning(
            f"⏰ Ready check {session_id} timed out after {READY_CHECK_TIMEOUT_SECONDS}s; "
            f"{len(non_responders)} non-responder(s): {non_responders}"
        )

        for user_id in non_responders:
            await SignUpHistory.record_ready_event(
                session_id=session_id,
                user_id=user_id,
                display_name=sign_ups.get(user_id, "Unknown user"),
                action="ready_timeout",
                guild_id=guild_id,
            )

        await self.delete_message(channel)
        state_manager.remove_ready_check(session_id)

        names = ", ".join(sign_ups.get(uid, "Unknown user") for uid in non_responders)
        try:
            await channel.send(
                f"⏰ **Ready check timed out.** {len(non_responders)} player(s) did not respond "
                f"({names}). Press **Ready Check** to start a new one when everyone's around."
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to send ready-check timeout notice for {session_id}: {e}")

    # --- class-level entry points (fetch session from state, delegate to instance) ---

    @classmethod
    async def cleanup(cls, session_id: str, channel) -> None:
        """Delete the ready check message and remove the session from state."""
        rc = state_manager.get_ready_check(session_id)
        if rc:
            await rc.delete_message(channel)
        state_manager.remove_ready_check(session_id)

    @classmethod
    async def cancel(cls, session_id: str, channel, cancelled_by: str) -> None:
        """Abort the ready check: delete the message, drop the state, and announce it.

        The draft message's Ready Check button is never disabled (the debounce in
        views.py prevents spam instead), so there is nothing to re-enable here.
        """
        await cls.cleanup(session_id, channel)
        await channel.send(f"**{cancelled_by}** cancelled the ready check.")

    @classmethod
    async def sync_added_player(cls, session_id: str, user_id: str, draft_session, interaction: discord.Interaction) -> None:
        """Add a newly joined player to the active ready check and refresh the embed."""
        rc = state_manager.get_ready_check(session_id)
        if not rc:
            return
        rc.add_player(user_id)
        await rc.refresh_embed(interaction.channel, draft_session.sign_ups, guild=interaction.guild)

    @classmethod
    async def sync_removed_player(cls, session_id: str, user_id: str, draft_session, interaction: discord.Interaction) -> None:
        """Remove a player from the active ready check, refresh the embed, and trigger auto-create if all ready."""
        rc = state_manager.get_ready_check(session_id)
        if not rc:
            return
        rc.remove_player(user_id)
        updated = await rc.refresh_embed(interaction.channel, draft_session.sign_ups, guild=interaction.guild)
        if updated and rc.all_ready():
            await cls.handle_all_ready(session_id, draft_session, interaction)

    @classmethod
    async def handle_all_ready(cls, session_id: str, draft_session, interaction: discord.Interaction) -> None:
        """Update the ready check embed to the all-ready state, then create teams."""
        rc = state_manager.get_ready_check(session_id)
        if rc:
            await rc.refresh_embed(
                interaction.channel,
                draft_session.sign_ups,
                guild=interaction.guild,
                title="✅ Ready Check Complete - All Players Ready!"
            )

        if state_manager.is_creating_teams(session_id):
            logger.warning(f"Teams already being created for {session_id}")
            return

        state_manager.set_creating_teams(session_id, True)
        try:
            await interaction.channel.send("✅ **All players ready!** Creating teams now...")

            bot = interaction.client
            if not (draft_session and draft_session.message_id and draft_session.draft_channel_id):
                return

            channel = bot.get_channel(int(draft_session.draft_channel_id))
            if not channel:
                return

            message = await channel.fetch_message(int(draft_session.message_id))

            # Deferred to avoid circular import: views -> ready_check -> views
            from views import PersistentView

            persistent_view = PersistentView(
                bot=bot,
                draft_session_id=session_id,
                session_type=draft_session.session_type,
                team_a_name=draft_session.team_a_name,
                team_b_name=draft_session.team_b_name
            )

            class _ChannelInteraction:
                def __init__(self, original, msg):
                    self.user = original.user
                    self.guild = original.guild
                    self.guild_id = original.guild_id
                    self.channel = original.channel
                    self.client = original.client
                    self.message = msg
                    self._followup = original.followup

                @property
                def followup(self):
                    return self._followup

            mock_interaction = _ChannelInteraction(interaction, message)
            success = await create_and_display_teams(bot, session_id, mock_interaction, persistent_view)
            if success:
                await interaction.channel.send("✅ Teams created! Check the draft message above for teams and seating order.")
            else:
                await interaction.channel.send("❌ Error creating teams. You can try using the Create Teams button manually.")
        except Exception as e:
            logger.error(f"Error auto-creating teams after ready check: {e}")
            await interaction.channel.send(f"❌ Error creating teams: {str(e)}\nYou can try using the Create Teams button manually.")
        finally:
            state_manager.set_creating_teams(session_id, False)


class ReadyCheckView(discord.ui.View):
    def __init__(self, draft_session_id):
        super().__init__(timeout=None)
        self.draft_session_id = draft_session_id
        self.ready_button.custom_id = f"ready_check_ready_{self.draft_session_id}"
        self.not_ready_button.custom_id = f"ready_check_not_ready_{self.draft_session_id}"
        self.cancel_button.custom_id = f"ready_check_cancel_{self.draft_session_id}"

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.green, custom_id="placeholder_ready")
    async def ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle_status(interaction, "ready")

    @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red, custom_id="placeholder_not_ready")
    async def not_ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle_status(interaction, "not_ready")

    @discord.ui.button(label="Cancel Check", style=discord.ButtonStyle.grey, custom_id="placeholder_cancel_rc")
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Are you sure you want to cancel the ready check?",
            view=ReadyCheckCancelConfirmView(self.draft_session_id, interaction.user.display_name),
            ephemeral=True,
        )

    async def _handle_status(self, interaction: discord.Interaction, status: PlayerStatus) -> None:
        rc = state_manager.get_ready_check(self.draft_session_id)
        if not rc:
            logger.warning(
                f"Ready click against missing session {self.draft_session_id} "
                f"by user {interaction.user.id}; live checks: "
                f"{list(state_manager.ready_checks.keys())}"
            )
            await interaction.response.send_message("Session data is missing.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if not rc.has_player(user_id):
            logger.warning(
                f"Unauthorized ready click on {self.draft_session_id} by user {user_id} (not a participant)"
            )
            await interaction.response.send_message("You are not authorized to interact with this button.", ephemeral=True)
            return

        rc.set_status(user_id, status)

        draft_session = await get_draft_session(self.draft_session_id)

        # Record the response in the audit trail. Auditing is best-effort: a DB/audit
        # failure must not break the user's ready click, so swallow and log it.
        try:
            await SignUpHistory.record_ready_event(
                session_id=self.draft_session_id,
                user_id=user_id,
                display_name=(draft_session.sign_ups or {}).get(user_id, "Unknown user"),
                action=status,
                guild_id=str(interaction.guild.id),
            )
        except Exception as e:
            logger.error(f"Failed to record ready event for {self.draft_session_id} user {user_id}: {e}")

        logger.info(
            f"Ready click on {self.draft_session_id}: user {user_id} -> {status}; "
            f"counts={rc.counts()}; complete={rc.all_ready()}"
        )

        embed = await rc.build_embed(draft_session.sign_ups, guild=interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        if rc.all_ready():
            await ReadyCheckSession.handle_all_ready(self.draft_session_id, draft_session, interaction)


class ReadyCheckCancelConfirmView(discord.ui.View):
    def __init__(self, draft_session_id: str, cancelled_by: str):
        super().__init__(timeout=60)
        self.draft_session_id = draft_session_id
        self.cancelled_by = cancelled_by

    @discord.ui.button(label="Yes, Cancel", style=discord.ButtonStyle.danger)
    async def confirm_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await ReadyCheckSession.cancel(
            self.draft_session_id,
            interaction.channel,
            cancelled_by=self.cancelled_by,
        )

    @discord.ui.button(label="No, Keep Going", style=discord.ButtonStyle.secondary)
    async def deny_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
