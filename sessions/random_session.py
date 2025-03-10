from .base_session import BaseSession
from discord import Embed, Color

class RandomSession(BaseSession):
    def create_embed(self):
        title = f"Looking for Players! {self.session_details.cube_choice} Random Team Draft - Queue Opened <t:{self.session_details.draft_start_time}:R>"
        description = (
            "**How to use bot**:\n"
            "1. Click sign up and click the draftmancer link.\n"
            "2. When enough people join (6 or 8), push Ready Check. Once everyone is ready, push Create Teams.\n"
            "3. Create Teams will create random teams and a corresponding seating order. Draftmancer host needs "
            "to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER**\n"
            f"**Chosen Cube: [{self.session_details.cube_choice}](https://cubecobra.com/cube/list/{self.session_details.cube_choice})**\n"
            f"**Draftmancer Session**: **[Join Here]({self.session_details.draft_link})**"
        )
        embed = Embed(title=title, description=description, color=Color.dark_magenta())
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        # thumbnail by chosen cube?
        cube_choice = self.session_details.cube_choice
        if cube_choice == "LSVCube":
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1239255966818635796/1348496824956354641/LSVCube.png?ex=67cfad08&is=67ce5b88&hm=16d3350410a3a4c87404c5e6fa1c8ce0408db78a6e841a9133fd69886a1a0db8&")
        elif cube_choice == "LSVRetro":
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1239255966818635796/1348496825417470012/LSVRetro.png?ex=67cfad09&is=67ce5b89&hm=8d4d755e1e47993910f06f886f131b2f7930a8fff022db7651ca3e976d1582ce&")
        else:
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")
        return embed

    def get_session_type(self):
        return "random"
