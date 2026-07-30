"""Selection policy for /cleanup_stale_drafts: which sessions are stale.

"Stale" means fired-but-abandoned, never merely old. Finished drafts routinely
keep session_stage='pairings' forever (only ~2 in 10 historical rows ever see
'completed'), so the posted victory/draw message is the load-bearing finish
marker alongside the stage.

The command fetches candidates by guild + time window in SQL, then applies
is_stale_draft in Python so this module is the single source of truth for
what "stale" means (and is unit-testable without a DB).
"""
from datetime import datetime, timedelta

# Only chase recently-stuck drafts; anything older is history, not cleanup.
LOOKBACK_DAYS = 30


def cleanup_window(hours_old: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(floor, cutoff): candidates started before `cutoff` (older than
    `hours_old`) but on/after `floor` (within LOOKBACK_DAYS)."""
    now = now or datetime.now()
    return now - timedelta(days=LOOKBACK_DAYS), now - timedelta(hours=hours_old)


def is_finished_draft(session) -> bool:
    """A draft with any completion marker: explicit completed stage or a
    posted victory/draw message (the common case — the stage rarely
    advances past 'pairings' even for fully played drafts)."""
    return (
        session.session_stage == "completed"
        or session.victory_message_id_draft_chat is not None
        or session.victory_message_id_results_channel is not None
    )


def is_stale_draft(session) -> bool:
    """Fired (teams were created) but never finished. Sessions that never
    fired are the queue-inactivity cleanup's job, not this command's."""
    return session.teams_start_time is not None and not is_finished_draft(session)
