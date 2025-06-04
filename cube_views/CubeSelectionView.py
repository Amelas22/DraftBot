import discord

class CubeUpdateSelectionView(discord.ui.View):
    """Selection view for updating cubes, to avoid circular imports with modals.py"""
    def __init__(self, session_type: str):
        super().__init__()
        self.session_type = session_type
        
        # Define cube options by type
        cube_options = {
            "winston": [
                discord.SelectOption(label="LSVWinston", value="LSVWinston"),
                discord.SelectOption(label="ChillWinston", value="ChillWinston"),
                discord.SelectOption(label="WinstonDeluxe", value="WinstonDeluxe"),
                discord.SelectOption(label="data", value="data")
            ],
            "default": [
                discord.SelectOption(label="LSVCube", value="LSVCube"),
                discord.SelectOption(label="AlphaFrog", value="AlphaFrog"),
                discord.SelectOption(label="2025 MOCS Vintage Cube", value="APR25"),
                discord.SelectOption(label="LSVRetro", value="LSVRetro"),
                discord.SelectOption(label="PowerMack", value="PowerMack"),
                discord.SelectOption(label="Powerslax", value="Powerslax"),
                discord.SelectOption(label="PowerSam", value="PowerSam"),
            ]
        }
        
        # Get the appropriate options for this session type
        options = cube_options.get(session_type, cube_options["default"])
        
        # Create the select dropdown with those options
        self.cube_select = discord.ui.Select(
            placeholder="Select a Cube",
            options=options
        )
        
        # We'll set the callback later in the update_cube_callback method
        self.add_item(self.cube_select)