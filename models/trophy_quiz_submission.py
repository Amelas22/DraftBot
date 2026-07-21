from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, text, JSON
from database.models_base import Base


class TrophyQuizSubmission(Base):
    __tablename__ = 'trophy_quiz_submissions'

    quiz_id = Column(String(64), ForeignKey('trophy_quiz_sessions.quiz_id'), primary_key=True)
    player_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    guesses = Column(JSON, nullable=False)            # [winsA, winsB]
    direction_correct = Column(Boolean, nullable=False, default=False)
    exact_points = Column(JSON, nullable=False)       # [ptsA, ptsB]
    points_earned = Column(Integer, nullable=False)
    submitted_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
