import discord
from sqlalchemy import Column, Integer, String, DateTime, JSON, select, Boolean, ForeignKey, desc, Float, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, relationship
from database.models_base import Base
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(level=logging.WARNING)  # Adjust the application-wide logging level
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)  # Specifically reduce SQLAlchemy logging verbosity

DATABASE_URL = "sqlite+aiosqlite:///drafts.db" 

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

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
    swiss_matches = Column(JSON)
    draft_data = Column(JSON)
    data_received = Column(Boolean, default=False)
    cube = Column(String(128))
    betting_market_ids = Column(JSON)  
    betting_team_message_id = Column(String(64))  
    betting_trophy_message_id = Column(String(64))  
    betting_close_time = Column(DateTime)  
    
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
    PreseasonPoints = Column(Integer, default=0)

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
    TeamName = Column(String(128), nullable=False)
    WeekStartDate = Column(DateTime, nullable=False)
    MatchesPlayed = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)

class PlayerLimit(Base):
    __tablename__ = 'player_limits'

    player_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))  
    drafts_participated = Column(Integer, default=0)
    WeekStartDate = Column(DateTime, nullable=False, primary_key=True)
    match_one_points = Column(Integer, default=0)
    match_two_points = Column(Integer, default=0)
    match_three_points = Column(Integer, default=0)
    match_four_points = Column(Integer, default=0)

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

class SwissChallenge(Base):
    __tablename__ = 'swiss_challenges'
    
    id = Column(Integer, primary_key=True)
    initial_user = Column(String(64))
    sign_ups = Column(JSON)
    message_id = Column(String(64), nullable=True)
    channel_id = Column(String(64), nullable=True)
    guild_id = Column(String(64))
    start_time = Column(DateTime, nullable=False)
    cube = Column(String(128))

class TeamFinder(Base):
    __tablename__ = 'team_finder'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False)
    display_name = Column(String(128), nullable=False)
    timezone = Column(String(64), nullable=False)
    message_id = Column(String(64))
    channel_id = Column(String(64))
    guild_id = Column(String(64))

class UserWallet(Base):
    __tablename__ = 'user_wallets'
    
    user_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    balance = Column(Integer, default=1000)  # Start with 1000 coins
    last_daily_claim = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<UserWallet(user_id={self.user_id}, display_name={self.display_name}, balance={self.balance})>"

class BettingMarket(Base):
    __tablename__ = 'betting_markets'
    
    id = Column(Integer, primary_key=True)
    draft_session_id = Column(String(64), ForeignKey('draft_sessions.session_id'))
    market_type = Column(String(32))  # 'team_win', 'player_trophy'
    status = Column(String(32), default='open')  # 'open', 'closed', 'resolved'
    created_at = Column(DateTime, default=datetime.now)
    
    # For team win markets
    team_a_odds = Column(Float)
    team_b_odds = Column(Float)
    draw_odds = Column(Float, nullable=True)  # Only for 8-player drafts
    
    # For player trophy markets
    player_id = Column(String(64), nullable=True)
    player_name = Column(String(128), nullable=True)
    trophy_odds = Column(Float, nullable=True)
    
    # Winner info
    winning_outcome = Column(String(32), nullable=True)  # 'team_a', 'team_b', 'draw', 'trophy', 'no_trophy'
    
    # Relationships
    bets = relationship("UserBet", back_populates="market")
    
    def __repr__(self):
        return f"<BettingMarket(id={self.id}, draft_session_id={self.draft_session_id}, market_type={self.market_type}, status={self.status})>"

class UserBet(Base):
    __tablename__ = 'user_bets'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64))
    display_name = Column(String(128))
    market_id = Column(Integer, ForeignKey('betting_markets.id'))
    bet_amount = Column(Integer)
    selected_outcome = Column(String(32))  # 'team_a', 'team_b', 'draw', 'trophy', 'no_trophy'
    odds_at_bet_time = Column(Float)
    placed_at = Column(DateTime, default=datetime.now)
    status = Column(String(32), default='active')  # 'active', 'won', 'lost'
    potential_payout = Column(Integer)
    
    # Relationships
    market = relationship("BettingMarket", back_populates="bets")
    
    def __repr__(self):
        return f"<UserBet(id={self.id}, user_id={self.user_id}, market_id={self.market_id}, bet_amount={self.bet_amount}, selected_outcome={self.selected_outcome})>"
    
async def get_draft_session(session_id: str):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            query = select(DraftSession).filter_by(session_id=session_id)
            result = await session.execute(query)
            draft_session = result.scalars().first()
            return draft_session
        
async def re_register_views(bot):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Order the DraftSessions by their id in descending order to get the most recent ones
            stmt = select(DraftSession).order_by(desc(DraftSession.id)).limit(10)
            result = await session.execute(stmt)
            draft_sessions = result.scalars().all()

    for draft_session in draft_sessions:
        if draft_session.draft_channel_id and draft_session.message_id:
            channel_id = int(draft_session.draft_channel_id)
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(int(draft_session.message_id))
                    from views import PersistentView
                    view = PersistentView(bot=bot,
                                          draft_session_id=draft_session.session_id,
                                          session_type=draft_session.session_type,
                                          team_a_name=draft_session.team_a_name,
                                          team_b_name=draft_session.team_b_name)
                    await message.edit(view=view)  # Reattach the view
                except discord.NotFound:
                    # Handle cases where the message or channel might have been deleted
                    print(f"Message or channel not found for session: {draft_session.session_id}")
                except Exception as e:
                    # Log or handle any other exceptions
                    print(f"Failed to re-register view for session: {draft_session.session_id}, error: {e}")
        else:
            # Log or handle sessions without a valid channel or message ID
            print(f"Session {draft_session.session_id} does not have a valid channel and/or message ID.")


async def register_team_to_db(team_name: str):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Normalize the team name for case-insensitive comparison
            normalized_team_name = team_name.strip().lower()
            # Check if the team already exists
            query = select(Team).filter(func.lower(Team.TeamName) == normalized_team_name)
            result = await session.execute(query)
            existing_team = result.scalars().first()

            if existing_team:
                return existing_team, f"Team '{existing_team.TeamName}' is already registered."

            # If not exists, create and register the new team
            new_team = Team(TeamName=team_name)
            session.add(new_team)
            await session.commit()

            return new_team, f"Team '{team_name}' has been registered successfully."

async def remove_team_from_db(ctx, team_name: str):
    # Check if the user has the "cube overseer" role
    if any(role.name == "Cube Overseer" for role in ctx.author.roles):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Normalize the team name for case-insensitive comparison
                normalized_team_name = team_name.strip().lower()
                # Check if the team exists
                query = select(Team).filter(func.lower(Team.TeamName) == normalized_team_name)
                result = await session.execute(query)
                existing_team = result.scalars().first()

                if not existing_team:
                    await ctx.send(f"Team '{team_name}' does not exist.")
                    return

                # If exists, delete the team
                await session.delete(existing_team)
                await session.commit()

                return f"Team '{team_name}' has been removed"
    else:
        return "You do not have permission to remove a team. This action requires the 'cube overseer' role."
