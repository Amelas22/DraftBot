"""Which library a server draws on.

One row per server, and the guild is the key: a server draws on exactly one
library. That is deliberately narrower than the model allows in the other
direction -- a library serves as many servers as it likes -- because it makes
every "which library is this?" question a single lookup from the guild the
command was typed in, with no cube or cog needing to disambiguate.

A server with no row has no library. That is the off state, and it is not the
same as being bound to an empty one: absence means nobody has said this server
may borrow, which is the answer that lends nothing.

In the database rather than `configs/<guild>.json` because guild config is
writable by a server admin through the bot's own commands. A server able to
repoint itself at a cheaper library would be able to set its own deposit,
which is the one thing the shell-tool rule exists to prevent.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String

from database.models_base import Base


class LibraryServer(Base):
    __tablename__ = 'library_server'

    guild_id = Column(String(64), primary_key=True)
    library_id = Column(String(64), nullable=False)
    bound_at = Column(DateTime, default=datetime.now)
    bound_by = Column(String(64), nullable=True)
