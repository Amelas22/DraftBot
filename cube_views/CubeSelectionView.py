import discord
from config import get_cube_options

class CubeUpdateSelectionView(discord.ui.View):
    """Selection view for updating cubes, to avoid circular imports with modals.py"""
    def __init__(self, session_type: str, guild_id: int):
        super().__init__()
        self.session_type = session_type

        options = [discord.SelectOption(**opt) for opt in get_cube_options(guild_id, session_type)]
        
        # Create the select dropdown with those options
        self.cube_select = discord.ui.Select(
            placeholder="Select a Cube",
            options=options
        )
        
        # We'll set the callback later in the update_cube_callback method
        self.add_item(self.cube_select)