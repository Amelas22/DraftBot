import discord
from discord.ui import Button, View
from sqlalchemy import select
from session import AsyncSessionLocal, TeamFinder

TIMEZONES_AMERICAS = [
    ("Pacific Time (UTC-08:00)", "America/Los_Angeles"),
    ("Mountain Time (UTC-07:00)", "America/Denver"),
    ("Central Time (UTC-06:00)", "America/Chicago"),
    ("Eastern Time (UTC-05:00)", "America/New_York"),
    ("Atlantic Time (UTC-04:00)", "America/Halifax"),
    ("Brasilia Time (UTC-03:00)", "America/Sao_Paulo"),
    ("Fernando de Noronha Time (UTC-02:00)", "America/Noronha")
]

TIMEZONES_EUROPE = [
    ("Western European Summer Time (UTC+01:00)", "Europe/Lisbon"),
    ("British Summer Time (UTC+01:00)", "Europe/London"),
    ("Central European Summer Time (UTC+02:00)", "Europe/Berlin"),
    ("Eastern European Summer Time (UTC+03:00)", "Europe/Helsinki")
]

TIMEZONES_ASIA_AUSTRALIA = [
    ("Gulf Standard Time (UTC+04:00)", "Asia/Dubai"),
    ("Pakistan Standard Time (UTC+05:00)", "Asia/Karachi"),
    ("Indian Standard Time (UTC+05:30)", "Asia/Kolkata"),
    ("Indochina Time (UTC+07:00)", "Asia/Bangkok"),
    ("China Standard Time (UTC+08:00)", "Asia/Shanghai"),
    ("Japan Standard Time (UTC+09:00)", "Asia/Tokyo"),
    ("Australian Eastern Standard Time (UTC+10:00)", "Australia/Sydney"),
    ("New Zealand Standard Time (UTC+12:00)", "Pacific/Auckland")
]

TIMEZONE_LABEL_TO_VALUE = {label: value for region in [TIMEZONES_AMERICAS, TIMEZONES_EUROPE, TIMEZONES_ASIA_AUSTRALIA] for label, value in region}

class TimezoneButton(Button):
    def __init__(self, label, value, message_id):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.value = value
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        timezone = self.value
        display_name = interaction.user.display_name

        async with AsyncSessionLocal() as session:
            stmt = select(TeamFinder).where(TeamFinder.user_id == user_id)
            result = await session.execute(stmt)
            record = result.scalars().first()

            if record and record.timezone == timezone:
                # Remove the record if the user clicks the same timezone again
                await session.delete(record)
                action = "removed from"
            else:
                if record:
                    # Update existing record
                    record.timezone = timezone
                    record.message_id = self.message_id
                    record.display_name = display_name
                else:
                    # Create new record
                    new_record = TeamFinder(
                        user_id=user_id,
                        display_name=display_name,
                        timezone=timezone,
                        message_id=self.message_id,
                        channel_id=str(interaction.channel_id),
                        guild_id=str(interaction.guild_id)
                    )
                    session.add(new_record)
                action = "added to"

            await session.commit()

        # Update the embed with the new user
        await update_embed(interaction.message, self.message_id)

         # Send a confirmation message
        await interaction.response.send_message(f"You have been {action} the timezone: {self.label}", ephemeral=True)

async def update_embed(message, message_id):
    embed = message.embeds[0]  # Get the current embed

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for i, field in enumerate(embed.fields):
                timezone_label = field.name
                timezone_value = TIMEZONE_LABEL_TO_VALUE.get(timezone_label)

                stmt = select(TeamFinder).where(TeamFinder.timezone == timezone_value, TeamFinder.message_id == message_id)
                result = await session.execute(stmt)
                records = result.scalars().all()

                mentions = "\n".join([f"<@{record.user_id}>" for record in records]) or "No Sign-ups yet"
                embed.set_field_at(i, name=timezone_label, value=mentions, inline=False)

    await message.edit(embed=embed)

def create_view(timezones, message_id):
    view = View(timeout=None)
    for label, value in timezones:
        button = TimezoneButton(label=label, value=value, message_id=message_id)
        view.add_item(button)
    return view

async def re_register_teamfinder(bot):
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(TeamFinder.message_id).distinct()
            result = await db_session.execute(stmt)
            message_ids = result.scalars().all()

            for message_id in message_ids:
                stmt = select(TeamFinder).where(TeamFinder.message_id == message_id)
                result = await db_session.execute(stmt)
                record = result.scalars().first()

                if record and record.channel_id and record.guild_id:
                    channel = bot.get_channel(int(record.channel_id))
                    if channel:
                        try:
                            message = await channel.fetch_message(int(record.message_id))
                            timezones = []

                            if "Americas" in message.embeds[0].title:
                                timezones = TIMEZONES_AMERICAS
                            elif "Europe" in message.embeds[0].title:
                                timezones = TIMEZONES_EUROPE
                            elif "Asia/Australia" in message.embeds[0].title:
                                timezones = TIMEZONES_ASIA_AUSTRALIA

                            view = create_view(timezones, record.message_id)
                            await message.edit(view=view)
                        except discord.NotFound:
                            print(f"Message or channel not found for teamfinder entry: {record.id}")
                        except Exception as e:
                            print(f"Failed to re-register view for teamfinder entry: {record.id}, error: {e}")