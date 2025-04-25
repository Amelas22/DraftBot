from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database.models_base import Base
from datetime import datetime

class LogChannel(Base):
    __tablename__ = 'log_channels'
    
    channel_id = Column(String(64), primary_key=True)
    guild_id = Column(String(64), nullable=False)
    last_post = Column(DateTime, nullable=True)
    time_zone = Column(String(64), default="UTC")
    
    # Relationships
    backup_logs = relationship("BackupLog", back_populates="channel", cascade="all, delete-orphan")
    user_submissions = relationship("UserSubmission", back_populates="channel", cascade="all, delete-orphan")
    post_schedules = relationship("PostSchedule", back_populates="channel", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<LogChannel(channel_id={self.channel_id})>"

class PostSchedule(Base):
    __tablename__ = 'post_schedules'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String(64), ForeignKey('log_channels.channel_id'), nullable=False)
    post_time = Column(String(10), nullable=False)  # Format: "HH:MM"
    
    # Relationship
    channel = relationship("LogChannel", back_populates="post_schedules")
    
    def __repr__(self):
        return f"<PostSchedule(id={self.id}, post_time={self.post_time})>"

class BackupLog(Base):
    __tablename__ = 'backup_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(512), nullable=False)
    added_by = Column(String(64), nullable=False)
    added_on = Column(DateTime, default=datetime.now)
    channel_id = Column(String(64), ForeignKey('log_channels.channel_id'), nullable=False)
    used = Column(Boolean, default=False)
    cube = Column(String(128), nullable=True)  
    record = Column(String(16), nullable=True)  
    
    # Relationship
    channel = relationship("LogChannel", back_populates="backup_logs")
    
    def __repr__(self):
        return f"<BackupLog(id={self.id}, used={self.used})>"

class UserSubmission(Base):
    __tablename__ = 'user_submissions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(512), nullable=True)  
    submitted_by = Column(String(64), nullable=False)
    submitted_on = Column(DateTime, default=datetime.now)
    channel_id = Column(String(64), ForeignKey('log_channels.channel_id'), nullable=False)
    used = Column(Boolean, default=False)
    cube = Column(String(128), nullable=True)  
    record = Column(String(16), nullable=True)  
    
    # Relationship
    channel = relationship("LogChannel", back_populates="user_submissions")
    
    def __repr__(self):
        return f"<UserSubmission(id={self.id}, used={self.used})>"