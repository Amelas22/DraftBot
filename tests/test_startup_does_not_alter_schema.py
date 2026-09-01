"""Startup must not edit the schema. Migrations own it.

A bootstrap routine used to ALTER TABLE ... ADD COLUMN guild_id on four tables
every time the bot started, from before migrations were disciplined here. Three
of those tables declare guild_id in their models, so it did nothing. The fourth,
match_results, had the column deliberately dropped -- and the routine put it
straight back on the next boot.

That is why the schema and the migration history disagreed for months: the drop
ran in August, the bot undid it, and every `alembic revision --autogenerate`
since has proposed dropping it again, carrying other destructive lines along
for the ride.
"""
import inspect


def test_nothing_at_startup_issues_ddl():
    """The bot's startup path must not contain schema-altering SQL."""
    import bot
    import database.db_session as db

    for module in (bot, db):
        src = inspect.getsource(module)
        assert "ADD COLUMN" not in src.upper(), (
            f"{module.__name__} issues ADD COLUMN at import/startup; a migration "
            "that drops a column will be undone by the next restart")


def test_the_guild_id_bootstrap_is_gone():
    import database.db_session as db

    assert not hasattr(db, "ensure_guild_id_in_tables"), (
        "the startup guild_id bootstrap is back; it re-adds "
        "match_results.guild_id after every migration that drops it")


def test_match_results_still_has_no_guild_id_in_the_model():
    """The column the bootstrap kept resurrecting: a match belongs to whatever
    guild its draft session does."""
    from models.match import MatchResult

    assert "guild_id" not in MatchResult.__table__.columns
