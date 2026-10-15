# services/library_publish.py
"""Committed library writes publish themselves.

A refresh that every write site has to remember is a refresh somebody forgets,
so the session does it. before_flush notes which libraries a session's rows
touch; do_orm_execute notes bulk update()/delete() statements, which flush never
sees; after_commit publishes, so a listener only ever reads committed state; a
rollback forgets.

A bulk statement does not say which rows it touched, so it marks every library:
there are a handful, and the bus merges.

What it cannot see is a write that bypasses the session -- raw SQL text or an
engine connection. tests/test_library_publish.py fails the build on one.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from services import library_events as bus

_INFO = "library_publish"

TRACKED_DRAFT_COLUMNS = frozenset({
    "library_reserved_at", "library_released_at", "library_released", "library_agreed",
    "cube", "sign_ups", "session_stage", "teams_start_time",
    "victory_message_id_draft_chat", "victory_message_id_results_channel",
})
# Bulk statements on these tables publish only when they SET one of these columns.
_FILTERED_TABLES = {
    "draft_sessions": TRACKED_DRAFT_COLUMNS,
    "player_preferences": frozenset({"uses_library"}),
}
_ANY_WRITE_TABLES = frozenset({"card_loans", "debt_ledger", "library", "library_cube",
                               "library_server", "library_member"})

_installed = False


def _pending(session: Session) -> "dict[str, Any]":
    return session.info.setdefault(_INFO, {"libraries": set(), "guilds": set(), "all": False})


def _changed(obj: Any, columns: "frozenset[str]") -> bool:
    attrs = inspect(obj).attrs
    return any(attrs[c].history.has_changes() for c in columns if c in attrs)


def _scope_library(scope: Optional[str]) -> Optional[str]:
    if scope and scope.startswith("library:"):
        return scope.split(":", 1)[1] or None
    return None


def _note(session: Session, obj: Any, *, whole_row: bool) -> None:
    from models.card_loan import CardLoan
    from models.debt_ledger import DebtLedger
    from models.draft_session import DraftSession
    from models.library import Library
    from models.library_cube import LibraryCube
    from models.library_member import LibraryMember
    from models.library_server import LibraryServer
    from preference_service import PlayerPreferences

    p = _pending(session)
    if isinstance(obj, CardLoan):
        if obj.library_id:
            p["libraries"].add(str(obj.library_id))
        elif obj.guild_id:
            p["guilds"].add(str(obj.guild_id))
    elif isinstance(obj, DebtLedger):
        lid = _scope_library(obj.guild_id)
        if lid:
            p["libraries"].add(lid)
    elif isinstance(obj, DraftSession):
        if obj.guild_id and (whole_row or _changed(obj, TRACKED_DRAFT_COLUMNS)):
            p["guilds"].add(str(obj.guild_id))
    elif isinstance(obj, PlayerPreferences):
        if obj.guild_id and (whole_row or _changed(obj, frozenset({"uses_library"}))):
            p["guilds"].add(str(obj.guild_id))
    elif isinstance(obj, Library):
        p["libraries"].add(str(obj.id))
    elif isinstance(obj, (LibraryCube, LibraryMember, LibraryServer)):
        p["libraries"].add(str(obj.library_id))


def _before_flush(session: Session, _ctx: Any, _instances: Any) -> None:
    try:
        for obj in list(session.new) + list(session.deleted):
            _note(session, obj, whole_row=True)
        for obj in list(session.dirty):
            _note(session, obj, whole_row=False)
    except Exception:
        # Never fail a write over a board refresh.
        logger.opt(exception=True).error("library publish: could not classify a flush")


def _set_columns(statement: Any) -> "Optional[set[str]]":
    """The columns an ORM update() sets, or None if they can't be read -- in which
    case the caller assumes a tracked one, because a missed refresh is the failure
    this module exists to prevent. `_values` is SQLAlchemy's own record of
    .values(); there is no public accessor for it in 2.0."""
    values = getattr(statement, "_values", None)
    if not values:
        return None
    return {str(getattr(k, "key", None) or getattr(k, "name", None) or k) for k in values}


def _do_orm_execute(state: Any) -> None:
    try:
        if not (state.is_update or state.is_delete):
            return
        table = getattr(state.statement, "table", None)
        name = getattr(table, "name", None)
        if name in _ANY_WRITE_TABLES:
            _pending(state.session)["all"] = True
        elif name in _FILTERED_TABLES:
            cols = None if state.is_delete else _set_columns(state.statement)
            if cols is None or cols & _FILTERED_TABLES[name]:
                _pending(state.session)["all"] = True
    except Exception:
        logger.opt(exception=True).error("library publish: could not classify a statement")


def _after_commit(session: Session) -> None:
    p = session.info.pop(_INFO, None)
    if not p:
        return
    if p["all"]:
        bus.publish_all()
        return
    for lid in p["libraries"]:
        bus.publish(lid)
    for guild in p["guilds"]:
        bus.publish_for_guild(guild)


def _after_rollback(session: Session) -> None:
    session.info.pop(_INFO, None)


def install() -> None:
    """Register the hooks once, on every session the bot opens."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "do_orm_execute", _do_orm_execute)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _installed = True
