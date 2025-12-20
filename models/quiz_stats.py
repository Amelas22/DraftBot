from sqlalchemy import Column, Integer, String, Float, DateTime, text
from database.models_base import Base

class QuizStats(Base):
    __tablename__ = 'quiz_stats'

    # Composite primary key
    player_id = Column(String(64), primary_key=True)
    guild_id = Column(String(64), primary_key=True)

    # Player info
    display_name = Column(String(128))

    # Quiz participation
    total_quizzes = Column(Integer, default=0, server_default=text('0'))
    total_picks_attempted = Column(Integer, default=0, server_default=text('0'))
    total_picks_correct = Column(Integer, default=0, server_default=text('0'))
    accuracy_percentage = Column(Float, default=0.0, server_default=text('0.0'))

    # Point system
    total_points = Column(Integer, default=0, server_default=text('0'))
    average_points_per_quiz = Column(Float, default=0.0, server_default=text('0.0'))
    highest_quiz_score = Column(Integer, default=0, server_default=text('0'))

    # Streak tracking (consecutive perfect scores)
    current_perfect_streak = Column(Integer, default=0, server_default=text('0'))
    longest_perfect_streak = Column(Integer, default=0, server_default=text('0'))

    # Timestamp
    last_quiz_time = Column(DateTime, nullable=True)

    def __init__(self, **kwargs):
        """Initialize with sensible defaults"""
        super().__init__(**kwargs)
        # Set defaults for new objects (database defaults only apply on insert)
        if 'total_quizzes' not in kwargs:
            self.total_quizzes = 0
        if 'total_picks_attempted' not in kwargs:
            self.total_picks_attempted = 0
        if 'total_picks_correct' not in kwargs:
            self.total_picks_correct = 0
        if 'accuracy_percentage' not in kwargs:
            self.accuracy_percentage = 0.0
        if 'total_points' not in kwargs:
            self.total_points = 0
        if 'average_points_per_quiz' not in kwargs:
            self.average_points_per_quiz = 0.0
        if 'highest_quiz_score' not in kwargs:
            self.highest_quiz_score = 0
        if 'current_perfect_streak' not in kwargs:
            self.current_perfect_streak = 0
        if 'longest_perfect_streak' not in kwargs:
            self.longest_perfect_streak = 0

    def update_stats(self, correct_count: int, points_earned: int):
        """Update stats after a quiz submission"""
        self.total_quizzes += 1
        self.total_picks_attempted += 4
        self.total_picks_correct += correct_count

        # Update point totals
        self.total_points += points_earned
        self.average_points_per_quiz = self.total_points / self.total_quizzes

        # Track highest score
        if points_earned > self.highest_quiz_score:
            self.highest_quiz_score = points_earned

        # Update streak (all 4 exact matches = perfect)
        if correct_count == 4:
            self.current_perfect_streak += 1
            if self.current_perfect_streak > self.longest_perfect_streak:
                self.longest_perfect_streak = self.current_perfect_streak
        else:
            self.current_perfect_streak = 0

        # Recalculate accuracy
        if self.total_picks_attempted > 0:
            self.accuracy_percentage = (self.total_picks_correct / self.total_picks_attempted) * 100

    def __repr__(self):
        return f"<QuizStats(player_id={self.player_id}, total_points={self.total_points}, accuracy={self.accuracy_percentage:.1f}%)>"
