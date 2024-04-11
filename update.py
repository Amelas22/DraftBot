'''
legacy code
'''


from sqlalchemy import Column, Integer, String, DateTime, JSON, select, Boolean, ForeignKey, desc, Float, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.future import select
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import asyncio

# Your database setup
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()

# Initialize DB (ensure this is called somewhere if needed)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class DraftSession(Base):
    __tablename__ = 'draft_sessions'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), nullable=False, unique=True)
    message_id = Column(String(64))
    draft_channel_id = Column(String(64))
    true_skill_draft = Column(Boolean, default=False)
    ready_check_message_id = Column(String(64))
    draft_link = Column(String(256))
    ready_check_status = Column(JSON)
    draft_start_time = Column(DateTime, default=datetime)
    deletion_time = Column(DateTime)
    teams_start_time = Column(DateTime)
    draft_chat_channel = Column(String(64))
    guild_id = Column(String(64))
    draft_id = Column(String(64))
    trophy_drafters = Column(JSON)
    team_a = Column(JSON)
    team_b = Column(JSON)
    victory_message_id_draft_chat = Column(String(64))
    victory_message_id_results_channel = Column(String(64))
    winning_gap = Column(Integer)
    draft_summary_message_id = Column(String(64))
    matches = Column(JSON)
    match_counter = Column(Integer, default=1)
    sign_ups = Column(JSON)
    channel_ids = Column(JSON)
    session_type = Column(String(64))
    session_stage = Column(String(64))
    team_a_name = Column(String(128))
    team_b_name = Column(String(128))
    are_rooms_processing = Column(Boolean, default=False)
    premade_match_id = Column(String(128))
    tracked_draft = Column(Boolean, default=False)
    match_results = relationship("MatchResult", back_populates="draft_session", foreign_keys="[MatchResult.session_id]")
    def __repr__(self):
        return f"<DraftSession(session_id={self.session_id}, guild_id={self.guild_id})>"

class MatchResult(Base):
    __tablename__ = 'match_results'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id'))
    match_number = Column(Integer)
    player1_id = Column(String(64))
    player1_wins = Column(Integer, default=0)
    player2_id = Column(String(64))
    player2_wins = Column(Integer, default=0)
    winner_id = Column(String(64), nullable=True)
    pairing_message_id = Column(String(64))
    draft_session = relationship("DraftSession", back_populates="match_results")


class PlayerStats(Base):
    __tablename__ = 'player_stats'
    
    player_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))  
    drafts_participated = Column(Integer, default=0)
    games_won = Column(Integer, default=0)
    games_lost = Column(Integer, default=0)
    elo_rating = Column(Float, default=1200)
    true_skill_mu = Column(Float, default=25)
    true_skill_sigma = Column(Float, default=8.333)

    def __repr__(self):
        return f"<PlayerStats(player_id={self.player_id}, display_name={self.display_name}, drafts_participated={self.drafts_participated}, games_won={self.games_won}, games_lost={self.games_lost}, elo_rating={self.elo_rating})>"


class Team(Base):
    __tablename__ = 'teams'

    TeamID = Column(Integer, primary_key=True)
    TeamName = Column(String(128), unique=True, nullable=False)
    MatchesCompleted = Column(Integer, default=0)
    MatchWins = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)

class Match(Base):
    __tablename__ = 'matches'

    MatchID = Column(Integer, primary_key=True)
    TeamAID = Column(Integer)
    TeamBID = Column(Integer)
    TeamAWins = Column(Integer, default=0)
    TeamBWins = Column(Integer, default=0)
    DraftWinnerID = Column(Integer, default=None)
    MatchDate = Column(DateTime, default=datetime.now())
    TeamAName = Column(String(128))
    TeamBName = Column(String(128))

class WeeklyLimit(Base):
    __tablename__ = 'weekly_limits'

    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer, ForeignKey('teams.TeamID'))
    TeamName = Column(String(128), unique=True, nullable=False)
    WeekStartDate = Column(DateTime, nullable=False)
    MatchesPlayed = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)

class TeamRegistration(Base):
    __tablename__ = 'team_registration'

    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer)
    TeamName = Column(String(128), unique=True, nullable=False)
    TeamMembers = Column(JSON)


class Challenge(Base):
    __tablename__ = 'challenges'
    
    id = Column(Integer, primary_key=True)
    initial_user = Column(String(64))
    opponent_user = Column(String(64))
    message_id = Column(String(64), nullable=True)
    channel_id = Column(String(64), nullable=True)
    guild_id = Column(String(64))
    team_a_id = Column(Integer, nullable=False)
    team_b_id = Column(Integer, nullable=True)
    start_time = Column(DateTime, nullable=False)
    team_a = Column(String(128))
    team_b = Column(String(128))
    cube = Column(String(128))

async def update_team(session, weekly_limit_id, new_matches_played=None):
    async with session.begin():
        team_registration_stmt = select(WeeklyLimit).where(WeeklyLimit.ID == weekly_limit_id)
        team_registration_result = await session.execute(team_registration_stmt)
        team_registration = team_registration_result.scalars().first()
        
        if team_registration:
            if new_matches_played is not None:
                team_registration.MatchesPlayed = new_matches_played
                print(f"Matches Played for {team_registration.TeamName} updated to {new_matches_played}.")
                await session.commit()

        else:
            print(f"TeamRegistration with ID {weekly_limit_id} not found.")

async def main():
    async with AsyncSessionLocal() as session:
        await update_team(session, 3, new_matches_played=2)
        

if __name__ == "__main__":
    asyncio.run(main())