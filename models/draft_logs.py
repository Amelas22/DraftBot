from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, text, func, Text, TIMESTAMP
from sqlalchemy.orm import relationship
from database.models_base import Base
from datetime import datetime

class LogChannel(Base):
    __tablename__ = 'log_channels'
    
    channel_id = Column(Text, primary_key=True, nullable=True)
    guild_id = Column(Text, nullable=False)
    last_post = Column(TIMESTAMP, nullable=True)
    time_zone = Column(Text, default="UTC", server_default=text("'UTC'"))
    enabled = Column(Boolean, default=True, server_default=text('1'))

    # Relationships
    backup_logs = relationship("BackupLog", back_populates="channel", cascade="all, delete-orphan")
    user_submissions = relationship("UserSubmission", back_populates="channel", cascade="all, delete-orphan")
    post_schedules = relationship("PostSchedule", back_populates="channel", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<LogChannel(channel_id={self.channel_id})>"

class PostSchedule(Base):
    __tablename__ = 'post_schedules'
    
    id = Column(Integer, primary_key=True, nullable=True, autoincrement=True)
    channel_id = Column(Text, ForeignKey('log_channels.channel_id', ondelete='CASCADE'), nullable=False)
    post_time = Column(Text, nullable=False)  # Format: "HH:MM"
    
    # Relationship
    channel = relationship("LogChannel", back_populates="post_schedules")
    
    def __repr__(self):
        return f"<PostSchedule(id={self.id}, post_time={self.post_time})>"

class BackupLog(Base):
    __tablename__ = 'backup_logs'
    
    id = Column(Integer, primary_key=True, nullable=True, autoincrement=True)
    url = Column(Text, nullable=False)
    added_by = Column(Text, nullable=False)
    added_on = Column(TIMESTAMP, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'))
    channel_id = Column(Text, ForeignKey('log_channels.channel_id', ondelete='CASCADE'), nullable=False)
    used = Column(Boolean, default=False, server_default=text('0'))
    cube = Column(Text, nullable=True)  
    record = Column(Text, nullable=True)  
    
    # Relationship
    channel = relationship("LogChannel", back_populates="backup_logs")
    
    def __repr__(self):
        return f"<BackupLog(id={self.id}, used={self.used})>"

class UserSubmission(Base):
    __tablename__ = 'user_submissions'
    
    id = Column(Integer, primary_key=True, nullable=True, autoincrement=True)
    url = Column(Text, nullable=True)  
    submitted_by = Column(Text, nullable=False)
    submitted_on = Column(TIMESTAMP, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'))
    channel_id = Column(Text, ForeignKey('log_channels.channel_id', ondelete='CASCADE'), nullable=False)
    used = Column(Boolean, default=False, server_default=text('0'))
    cube = Column(Text, nullable=True)  
    record = Column(Text, nullable=True)  
    
    # Relationship
    channel = relationship("LogChannel", back_populates="user_submissions")
    
    def __repr__(self):
        return f"<UserSubmission(id={self.id}, used={self.used})>"