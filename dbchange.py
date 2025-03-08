import sys
import asyncio
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Float, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from datetime import datetime

# Adjust the database URL to match your actual database
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"  # Update this if needed

# Base class for new models
Base = declarative_base()

class BettingMarket(Base):
    __tablename__ = 'betting_markets'
    
    id = Column(Integer, primary_key=True)
    draft_session_id = Column(String(64))  # Foreign key to draft_sessions.session_id
    guild_id = Column(String(64), nullable=False)  # Added guild_id column
    market_type = Column(String(32))  # 'team_win', 'player_trophy'
    status = Column(String(32), default='open')  # 'open', 'closed', 'resolved', 'cancelled'
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

class UserWallet(Base):
    __tablename__ = 'user_wallets'
    
    # Make both user_id and guild_id part of the primary key
    user_id = Column(String(64), primary_key=True)
    guild_id = Column(String(64), primary_key=True)  # Added guild_id as part of primary key
    display_name = Column(String(128))
    balance = Column(Integer, default=1000)  # Start with 1000 coins
    last_daily_claim = Column(DateTime, nullable=True)

class UserBet(Base):
    __tablename__ = 'user_bets'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64))
    guild_id = Column(String(64), nullable=False)  # Added guild_id column
    display_name = Column(String(128))
    market_id = Column(Integer, ForeignKey('betting_markets.id'))
    bet_amount = Column(Integer)
    selected_outcome = Column(String(32))  # 'team_a', 'team_b', 'draw', 'trophy', 'no_trophy'
    odds_at_bet_time = Column(Float)
    placed_at = Column(DateTime, default=datetime.now)
    status = Column(String(32), default='active')  # 'active', 'won', 'lost', 'refunded'
    potential_payout = Column(Integer)
    
    # Relationships
    market = relationship("BettingMarket", back_populates="bets")

async def add_columns_to_draft_sessions():
    # Create a session for executing SQL
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        async with session.begin():
            # Check if columns already exist before adding them
            column_names = ["betting_market_ids", "betting_team_message_id", "betting_trophy_message_id", "betting_close_time"]
            
            # Get column names from draft_sessions table
            try:
                result = await session.execute(text("PRAGMA table_info(draft_sessions)"))
                existing_columns = {row[1] for row in result.fetchall()}
                
                # Add each column if it doesn't exist
                for column_name in column_names:
                    if column_name not in existing_columns:
                        column_type = "JSON" if column_name == "betting_market_ids" else "VARCHAR(64)" if column_name.endswith("_id") else "TIMESTAMP"
                        await session.execute(text(f"ALTER TABLE draft_sessions ADD COLUMN {column_name} {column_type}"))
                        print(f"Added {column_name} column to draft_sessions")
                    else:
                        print(f"Column {column_name} already exists")
            except Exception as e:
                print(f"Error checking/adding columns: {e}")
                raise

async def create_betting_tables():
    # Create engine
    engine = create_async_engine(DATABASE_URL, echo=False)
    
    # Check if tables exist first
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing_tables = [row[0] for row in result.fetchall()]
    
    # Create tables that don't exist yet
    async with engine.begin() as conn:
        if 'betting_markets' not in existing_tables:
            await conn.run_sync(lambda sync_conn: BettingMarket.__table__.create(sync_conn))
            print("Created betting_markets table")
        else:
            print("betting_markets table already exists")
            
        if 'user_wallets' not in existing_tables:
            await conn.run_sync(lambda sync_conn: UserWallet.__table__.create(sync_conn))
            print("Created user_wallets table")
        else:
            print("user_wallets table already exists")
            
        if 'user_bets' not in existing_tables:
            await conn.run_sync(lambda sync_conn: UserBet.__table__.create(sync_conn))
            print("Created user_bets table")
        else:
            print("user_bets table already exists")
    
    # Add new columns to draft_sessions table
    await add_columns_to_draft_sessions()
    
    print("Database migration completed successfully!")

async def main():
    try:
        await create_betting_tables()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())