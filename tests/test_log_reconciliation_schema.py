"""The reconciliation columns exist on the model with the right types."""
from datetime import datetime
from sqlalchemy import DateTime
from models.draft_session import DraftSession


def test_model_has_unlock_at_and_team_logs_posted_at():
    cols = DraftSession.__table__.columns
    assert "unlock_at" in cols
    assert "team_logs_posted_at" in cols
    assert isinstance(cols["unlock_at"].type, DateTime)
    assert isinstance(cols["team_logs_posted_at"].type, DateTime)
    assert cols["unlock_at"].nullable
    assert cols["team_logs_posted_at"].nullable


def test_instance_accepts_the_new_fields():
    ds = DraftSession(session_id="s", unlock_at=datetime(2026, 1, 1),
                      team_logs_posted_at=datetime(2026, 1, 2))
    assert ds.unlock_at == datetime(2026, 1, 1)
    assert ds.team_logs_posted_at == datetime(2026, 1, 2)
