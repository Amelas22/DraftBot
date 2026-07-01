import discord
from cube_views.pack_options import BaseCubeSelectionView, CustomCubeNameModal


class CubeUpdateSelectionView(BaseCubeSelectionView):
    """Cube selection for an existing draft (Update Cube).

    Identical selection experience to starting a draft (cube dropdown incl.
    Custom, Advanced Options, and a submit button). The actual update is
    delegated to ``on_submit(interaction, view)`` so this view stays free of
    heavy imports.
    """

    submit_label = "Update Cube"

    def __init__(self, session_type: str, guild_id: int, current_cube: str | None = None, on_submit=None):
        super().__init__(session_type, guild_id, current_cube)
        self.on_submit = on_submit

    async def submit_callback(self, interaction: discord.Interaction):
        if not self.cube_choice:
            await interaction.response.send_message(
                "❌ Please select a cube before updating.", ephemeral=True
            )
            return

        if self.cube_choice == "custom":
            await interaction.response.send_modal(CustomCubeNameModal(self, self.on_submit))
        else:
            await self.on_submit(interaction, self)  # pyrefly: ignore
