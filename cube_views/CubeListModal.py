import discord
from config import get_config, save_config
from loguru import logger


def parse_cube_list(raw: str):
    """Parse a cube list from a textarea string.

    Each non-empty line must be "Label : cube_id".
    Returns (cubes, errors) where cubes is a list of {"label", "value"} dicts
    and errors is a list of human-readable error strings for malformed lines.
    """
    cubes = []
    errors = []
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            errors.append(f"Line {i}: `{line}` — expected `Label : cube_id`")
        else:
            cubes.append({"label": parts[0], "value": parts[1]})
    return cubes, errors


class CubeListModal(discord.ui.Modal):
    def __init__(self, session_type: str, prefill: str):
        super().__init__(title=f"Set {session_type.capitalize()} Cube List")
        self.session_type = session_type
        self.add_item(discord.ui.InputText(
            label="Cubes (one per line: Label : cube_id)",
            placeholder="LSVCube : LSVCube\nAlphaFrog : AlphaFrog\nMODO Vintage Cube : modovintage",
            value=prefill,
            style=discord.InputTextStyle.long,
            required=True,
        ))

    async def callback(self, interaction: discord.Interaction):
        raw = self.children[0].value
        cubes, errors = parse_cube_list(raw)

        if errors:
            await interaction.response.send_message(
                "❌ Could not parse the following lines:\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        config = get_config(guild_id)
        config.setdefault("cubes", {})[self.session_type] = cubes
        save_config(guild_id)
        logger.info(f"Cube list for '{self.session_type}' updated for guild {interaction.guild.name} by {interaction.user.name}")
        lines = [f"• **{c['label']}** (`{c['value']}`)" for c in cubes]
        await interaction.response.send_message(
            f"✅ Updated **{self.session_type}** cube list ({len(cubes)} cubes):\n" + "\n".join(lines),
            ephemeral=True,
        )
