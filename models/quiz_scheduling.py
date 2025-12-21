from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, text, func, Text, TIMESTAMP
from sqlalchemy.orm import relationship
from database.models_base import Base
from datetime import datetime

class QuizChannel(Base):
    __tablename__ = 'quiz_channels'

    channel_id = Column(Text, primary_key=True, nullable=False)
    guild_id = Column(Text, nullable=False)
    last_post = Column(TIMESTAMP, nullable=True)
    time_zone = Column(Text, default="UTC", server_default=text("'UTC'"))
    enabled = Column(Boolean, default=True, server_default=text('1'))

    # Relationships
    quiz_schedules = relationship("QuizSchedule", back_populates="channel", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<QuizChannel(channel_id={self.channel_id})>"

class QuizSchedule(Base):
    __tablename__ = 'quiz_schedules'

    id = Column(Integer, primary_key=True, nullable=True, autoincrement=True)
    channel_id = Column(Text, ForeignKey('quiz_channels.channel_id', ondelete='CASCADE'), nullable=False)
    post_time = Column(Text, nullable=False)  # Format: "HH:MM"

    # Relationship
    channel = relationship("QuizChannel", back_populates="quiz_schedules")

    def __repr__(self):
        return f"<QuizSchedule(id={self.id}, post_time={self.post_time})>"
