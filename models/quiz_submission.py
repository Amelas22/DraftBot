from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, text, JSON
from database.models_base import Base

class QuizSubmission(Base):
    __tablename__ = 'quiz_submissions'

    # Composite primary key
    quiz_id = Column(String(64), ForeignKey('quiz_sessions.quiz_id'), primary_key=True)
    player_id = Column(String(64), primary_key=True)

    # Player info
    display_name = Column(String(128))

    # Guesses (JSON list of 4 card IDs)
    guesses = Column(JSON, nullable=False)  # ["card_id_1", "card_id_2", ...]

    # Results
    correct_count = Column(Integer, nullable=False)  # 0-4 (exact matches)
    pick_1_correct = Column(Boolean, nullable=False)
    pick_2_correct = Column(Boolean, nullable=False)
    pick_3_correct = Column(Boolean, nullable=False)
    pick_4_correct = Column(Boolean, nullable=False)

    # Point scoring (weighted + parity bonus + perfect bonus)
    points_earned = Column(Integer, nullable=False)  # Total points for this quiz
    pick_1_points = Column(Integer, nullable=False)  # 0, 1 (parity), or 2 (exact)
    pick_2_points = Column(Integer, nullable=False)  # 0, 1 (parity), or 3 (exact)
    pick_3_points = Column(Integer, nullable=False)  # 0, 1 (parity), or 4 (exact)
    pick_4_points = Column(Integer, nullable=False)  # 0, 1 (parity), or 5 (exact)

    # Timestamp
    submitted_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    @property
    def pick_results_array(self) -> list[bool]:
        """Returns pick results as an array."""
        return [
            self.pick_1_correct,
            self.pick_2_correct,
            self.pick_3_correct,
            self.pick_4_correct
        ]

    @property
    def pick_points_array(self) -> list[int]:
        """Returns pick points as an array."""
        return [
            self.pick_1_points,
            self.pick_2_points,
            self.pick_3_points,
            self.pick_4_points
        ]

    def __repr__(self):
        return f"<QuizSubmission(quiz_id={self.quiz_id}, player_id={self.player_id}, points={self.points_earned})>"
