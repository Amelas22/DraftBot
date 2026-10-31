"""Allowed-mentions policies that keep user-supplied text from pinging anyone.

Team names, display names and cube names are typed by users and end up in
message content. Left to the library default, a name containing @everyone or a
role mention would notify the whole server. SAFE_DEFAULT parses users only;
pings() additionally names the exact roles a message is meant to notify.
"""
from collections.abc import Iterable

import discord
from discord.abc import Snowflake

SAFE_DEFAULT = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=True)


def pings(*, roles: Iterable[str | int] = (),
          users: Iterable[str | int] | bool = True) -> discord.AllowedMentions:
    """Allow notifying exactly these roles (and users); never @everyone."""
    allowed_users: bool | list[Snowflake]
    if isinstance(users, bool):
        allowed_users = users
    else:
        allowed_users = [discord.Object(id=int(u)) for u in users]
    allowed_roles: list[Snowflake] = [discord.Object(id=int(r)) for r in roles]
    return discord.AllowedMentions(
        everyone=False,
        roles=allowed_roles,
        users=allowed_users)
