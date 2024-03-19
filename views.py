import discord
import asyncio
from discord.ui import Select, View
from discord import SelectOption
from sessions import sessions

class PersistentView(View):
    def __init__(self, session_id):
        super().__init__(timeout=None)
        self.session_id = session_id

        self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{session_id}_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Sign Up", style=discord.ButtonStyle.red, custom_id=f"{session_id}_cancel_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Draft", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_cancel_draft"))
        self.add_item(discord.ui.Button(label="Remove User", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_remove_user"))
        self.add_item(discord.ui.Button(label="Ready Check", style=discord.ButtonStyle.green, custom_id=f"{session_id}_ready_check"))
        self.add_item(discord.ui.Button(label="Create Teams", style=discord.ButtonStyle.blurple, custom_id=f"{session_id}_randomize_teams"))
        self.add_item(discord.ui.Button(label="Create Chat Rooms", style=discord.ButtonStyle.green, custom_id=f"{session_id}_draft_complete", disabled=True))
        self.add_item(discord.ui.Button(label="Post Pairings", style=discord.ButtonStyle.primary, custom_id=f"{session_id}_post_pairings", disabled=True))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session = sessions.get(self.session_id)
        if interaction.data['custom_id'] == f"{self.session_id}_sign_up":
            await self.sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_sign_up":
            await self.cancel_sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_draft":
            await self.cancel_draft_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_randomize_teams":
            await self.randomize_teams_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_draft_complete":
            await self.draft_complete_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_post_pairings":
            await self.post_pairings_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_ready_check":
            await self.ready_check_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_remove_user":
            await self.remove_user_button_callback(interaction)
            return False
        else:
            return False

        return True

    async def sign_up_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        # Check if the sign-up list is already full
        if len(session.sign_ups) >= 8:
            await interaction.response.send_message("The sign-up list is already full. No more players can sign up.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id in session.sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:
            # User is signing up
            session.sign_ups[user_id] = interaction.user.display_name
            # Confirm signup with draft link
            draft_link = session.draft_link  # Ensure you have the draft_link available in your session
            signup_confirmation_message = f"You are now signed up. Join Here: {draft_link}"
            await interaction.response.send_message(signup_confirmation_message, ephemeral=True)
            # Update the draft message to reflect the new list of sign-ups
            await session.update_draft_message(interaction)
       

    async def cancel_sign_up_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in session.sign_ups:
            # User is not signed up; inform them
            await interaction.response.send_message("You are not signed up!", ephemeral=True)
        else:
            # User is canceling their sign-up
            del session.sign_ups[user_id]
            await interaction.response.send_message("Your sign-up has been canceled.", ephemeral=True)
            # Update the draft message to reflect the change in sign-ups
            await session.update_draft_message(interaction)
        

    async def draft_complete_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild

        team_a_members = [guild.get_member(user_id) for user_id in session.team_a]
        team_b_members = [guild.get_member(user_id) for user_id in session.team_b]
        all_members = team_a_members + team_b_members

        team_a_members = [member for member in team_a_members if member]  # Filter out None
        team_b_members = [member for member in team_b_members if member]  # Filter out None

        tasks = [
            session.create_team_channel(guild, "Team-A", team_a_members, session.team_a, session.team_b),
            session.create_team_channel(guild, "Team-B", team_b_members, session.team_a, session.team_b),
            session.create_team_channel(guild, "Draft-chat", all_members, session.team_a, session.team_b)  # Assuming you want overseers in draft chat too
        ]
        await asyncio.gather(*tasks)

        for item in self.children:
            if isinstance(item, discord.ui.Button):  # Ensure it's a Button you're iterating over
                # Disable the "Create Chat Rooms" button after use
                if item.custom_id == f"{self.session_id}_draft_complete":
                    item.disabled = True
                # Enable the "Post Pairings" button
                elif item.custom_id == f"{self.session_id}_post_pairings":
                    item.disabled = False

        await interaction.edit_original_response(view=self)
        await session.update_draft_complete_message(interaction)
    
    async def ready_check_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if session:
            # Check if the user is in the sign-up list
            if interaction.user.id in session.sign_ups:
                # Proceed with the ready check
                await session.initiate_ready_check(interaction)

                # Disable the "Ready Check" button after use
                for item in self.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id == f"{self.session_id}_ready_check":
                        item.disabled = True
                        break  # Stop the loop once the button is found and modified

                # Ensure the view reflects the updated state with the button disabled
                await interaction.edit_original_response(view=self)
            else:
                # Inform the user they're not in the sign-up list, hence can't initiate a ready check
                await interaction.response.send_message("You must be signed up to initiate a ready check.", ephemeral=True)
        else:
            await interaction.response.send_message("Session not found.", ephemeral=True)



    async def cancel_draft_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        # Check if the user is in session.sign_ups or if session.sign_ups is empty
        if user_id in session.sign_ups or not session.sign_ups:
            # Delete the draft message and remove the session
            await interaction.message.delete()
            sessions.pop(self.session_id, None)
            await interaction.response.send_message("The draft has been canceled.", ephemeral=True)
        else:
            # If the user is not signed up and there are sign-ups present, inform the user
            await interaction.response.send_message("You cannot cancel this draft because you are not signed up.", ephemeral=True)
    
    async def remove_user_button_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        # Check if the user initiating the remove action is in the sign_ups
        if interaction.user.id not in session.sign_ups:
            await interaction.response.send_message("You are not authorized to remove users.", ephemeral=True)
            return

        # If the session exists and has sign-ups, and the user is authorized, proceed
        if session.sign_ups:
            options = [SelectOption(label=user_name, value=str(user_id)) for user_id, user_name in session.sign_ups.items()]
            view = UserRemovalView(session_id=self.session_id)
            await interaction.response.send_message("Select a user to remove:", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("No users to remove.", ephemeral=True)

    async def randomize_teams_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        if session.team_a is None or session.team_b is None:
            session.split_into_teams()

        # Generate names for display using the session's sign_ups dictionary
        team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
        team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
        seating_order = await session.generate_seating_order()

        # Create the embed message for displaying the teams and seating order
        embed = discord.Embed(
            title=f"Draft-{session.draft_id} is Ready!",
            description=f"**Draftmancer Session**: **[Join Here]({session.draft_link})** \n" +
                        "Host of Draftmancer must manually adjust seating as per below. **TURN OFF RANDOM SEATING SETTING IN DRAFMANCER**" +
                        "\n\n**AFTER THE DRAFT**, select Create Chat Rooms (give it five seconds to generate rooms) then select Post Pairings" +
                        "\nPost Pairings will take about 10 seconds to process. Only press once.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Team A", value="\n".join(team_a_display_names), inline=True)
        embed.add_field(name="Team B", value="\n".join(team_b_display_names), inline=True)
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)


        # Iterate over the view's children (buttons) to update their disabled status
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Enable "Post Pairings" and "Draft Complete" buttons
                if item.custom_id == f"{self.session_id}_draft_complete":
                    item.disabled = False
                else:
                    # Disable all other buttons
                    item.disabled = True

        # Respond with the embed and updated view
        await interaction.response.edit_message(embed=embed, view=self)

        
    
    async def post_pairings_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()  # Ensure there's enough time for operations

        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        for item in self.children:
            if item.custom_id == f"{self.session_id}_post_pairings":
                item.disabled = True
                break  # Stop the loop once the button is found and modified
        await interaction.edit_original_response(view=self)

        original_message_id = session.message_id
        original_channel_id = interaction.channel.id  
        draft_chat_channel_id = session.draft_chat_channel
        self.pairings = session.calculate_pairings()
        await session.move_message_to_draft_channel(session.bot, original_channel_id, original_message_id, draft_chat_channel_id)

        
        # Post pairings in the draft chat channel
        await session.post_pairings(interaction.guild, self.pairings)
        
        await interaction.followup.send("Pairings have been posted to the draft chat channel and the original message moved.", ephemeral=True)
    
    async def sign_up(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.sign_up_callback(interaction, interaction.user.id)

    async def cancel_sign_up(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.cancel_sign_up_callback(interaction, interaction.user.id)
        
    async def draft_complete(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.draft_complete_callback(interaction)

    async def cancel_draft(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.cancel_draft_callback(interaction)

    async def randomize_teams(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.randomize_teams_callback(interaction)

    async def post_pairings(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.post_pairings_callback(interaction)

class UserRemovalSelect(Select):
    def __init__(self, options: list[SelectOption], session_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs, placeholder="Choose a user to remove...", min_values=1, max_values=1, options=options)
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        user_id_to_remove = int(self.values[0])
        session = sessions.get(self.session_id)

        if user_id_to_remove in session.sign_ups:
            removed_user_name = session.sign_ups.pop(user_id_to_remove)
            await interaction.response.send_message(f"Removed {removed_user_name} from the draft.", ephemeral=False)
            
            # After removing a user, update the original message with the new sign-up list
            await session.update_draft_message(interaction)

            # Optionally, after sending a response, you may want to update or remove the select menu
            # This line will edit the message to only show the text, removing the select menu.
            await interaction.edit_original_response(content=f"Removed {removed_user_name} from the draft.", view=None)
        else:
            await interaction.response.send_message("User not found in sign-ups.", ephemeral=True)

class UserRemovalView(View):
    def __init__(self, session_id: str):
        super().__init__()
        session = sessions.get(session_id)
        if session and session.sign_ups:
            options = [SelectOption(label=user_name, value=str(user_id)) for user_id, user_name in session.sign_ups.items()]
            self.add_item(UserRemovalSelect(options=options, session_id=session_id))
