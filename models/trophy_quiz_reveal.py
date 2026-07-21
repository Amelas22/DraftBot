from sqlalchemy import Column, String, DateTime, ForeignKey, text
from database.models_base import Base


class TrophyQuizReveal(Base):
    """One row = a player paid to reveal the pilots on a trophy quiz. Persisted at
    click time so re-opening the quiz remembers it (the -2 penalty can't be dodged)."""
    __tablename__ = 'trophy_quiz_reveals'

    quiz_id = Column(String(64), ForeignKey('trophy_quiz_sessions.quiz_id'), primary_key=True)
    player_id = Column(String(64), primary_key=True)
    revealed_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
