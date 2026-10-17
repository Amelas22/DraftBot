"""A player's own settings in one server: loading them and flipping one.

No Discord UI here (player_settings_view draws them). Every value is read and
written through its existing preference_service getter and setter, so /settings
and the older single-purpose commands agree on what is stored.
"""
import enum
from dataclasses import dataclass

from preference_service import (
    get_anonymous_donors,
    get_hide_from_sr_ladder,
    get_player_dm_notification_preference,
    set_donation_anonymous,
    set_hide_from_sr_ladder,
    update_player_dm_notification_preference,
)


class Setting(enum.Enum):
    STAKE_CAP = "stake_cap"
    LIBRARY = "library"
    DMS = "dms"
    HIDE_FROM_LADDER = "hide_from_ladder"
    ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Settings:
    dms: bool
    hide_from_ladder: bool
    anonymous: bool
    stake_cap: bool | None = None    # None: not offered in this server
    library: bool | None = None      # uses_library as stored; None = not set
    library_offered: bool = False


async def _anonymous(player_id: str, guild_id: str) -> bool:
    return player_id in await get_anonymous_donors([player_id], guild_id)


async def load_settings(player_id: str, guild_id: str) -> Settings:
    return Settings(
        dms=bool(await get_player_dm_notification_preference(player_id, guild_id)),
        hide_from_ladder=await get_hide_from_sr_ladder(player_id, guild_id),
        anonymous=await _anonymous(player_id, guild_id),
    )


async def flip(player_id: str, guild_id: str, setting: Setting) -> bool | None:
    """Write the opposite of what is stored now, and return it.

    It reads the stored value rather than trusting the caller's copy, so a
    click on an older /settings message cannot write back a stale state.
    None if the save failed.
    """
    match setting:
        case Setting.DMS:
            new = not await get_player_dm_notification_preference(player_id, guild_id)
            saved = await update_player_dm_notification_preference(player_id, guild_id, new)
        case Setting.HIDE_FROM_LADDER:
            new = not await get_hide_from_sr_ladder(player_id, guild_id)
            saved = await set_hide_from_sr_ladder(player_id, guild_id, new)
        case Setting.ANONYMOUS:
            new = not await _anonymous(player_id, guild_id)
            saved = await set_donation_anonymous(player_id, guild_id, new)
        case Setting.STAKE_CAP | Setting.LIBRARY:
            raise NotImplementedError(setting)
    return new if saved else None
