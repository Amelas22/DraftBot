#!/usr/bin/env python3
"""
Script to update guild ID references in the DraftBot database.
Updates from special guild ID to personal guild ID using environment variables.
"""

import sqlite3
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Guild IDs from environment variables
SPECIAL_GUILD_ID = os.getenv("SPECIAL_GUILD_ID")
PERSONAL_GUILD_ID = os.getenv("TEST_GUILD_ID")

# Validate that environment variables are set
if not SPECIAL_GUILD_ID:
    print("❌ SPECIAL_GUILD_ID not found in environment variables")
    print("Please add SPECIAL_GUILD_ID to your .env file")
    sys.exit(1)

if not PERSONAL_GUILD_ID:
    print("❌ TEST_GUILD_ID not found in environment variables")
    print("Please add TEST_GUILD_ID to your .env file")
    sys.exit(1)

# Database file path
DB_PATH = Path(__file__).parent / "drafts.db"

def backup_database():
    """Create a backup of the database before making changes."""
    backup_path = DB_PATH.with_suffix(f".backup.before_guild_update")
    backup_path.write_bytes(DB_PATH.read_bytes())
    print(f"✅ Database backed up to: {backup_path}")
    return backup_path

def update_guild_references(conn):
    """Update all guild ID references in the database."""
    cursor = conn.cursor()
    
    # Tables and their guild_id columns to update
    tables_to_update = [
        "player_stats",
        "draft_sessions", 
        "match_results",
        "challenges",
        "swiss_challenges",
        "leaderboard_messages",
        "sign_up_history",
        "log_channels",
        "team_finder"
    ]
    
    total_updates = 0
    
    for table in tables_to_update:
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name=?
        """, (table,))
        
        if not cursor.fetchone():
            print(f"⚠️  Table '{table}' does not exist, skipping...")
            continue
            
        # Check if guild_id column exists
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'guild_id' not in columns:
            print(f"⚠️  Table '{table}' has no guild_id column, skipping...")
            continue
            
        # Count rows that will be updated
        cursor.execute(f"""
            SELECT COUNT(*) FROM {table} 
            WHERE guild_id = ?
        """, (SPECIAL_GUILD_ID,))
        
        count = cursor.fetchone()[0]
        
        if count == 0:
            print(f"ℹ️  No rows to update in table '{table}'")
            continue
            
        # Update the guild_id
        cursor.execute(f"""
            UPDATE {table} 
            SET guild_id = ? 
            WHERE guild_id = ?
        """, (PERSONAL_GUILD_ID, SPECIAL_GUILD_ID))
        
        updated_rows = cursor.rowcount
        total_updates += updated_rows
        print(f"✅ Updated {updated_rows} rows in table '{table}'")
    
    return total_updates

def verify_updates(conn):
    """Verify that the updates were successful."""
    cursor = conn.cursor()
    
    # Check for any remaining special guild ID references
    tables_to_check = [
        "player_stats", "draft_sessions", "match_results", "challenges",
        "swiss_challenges", "leaderboard_messages", "sign_up_history", 
        "log_channels", "team_finder"
    ]
    
    remaining_refs = 0
    
    for table in tables_to_check:
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name=?
        """, (table,))
        
        if not cursor.fetchone():
            continue
            
        # Check if guild_id column exists
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'guild_id' not in columns:
            continue
            
        cursor.execute(f"""
            SELECT COUNT(*) FROM {table} 
            WHERE guild_id = ?
        """, (SPECIAL_GUILD_ID,))
        
        count = cursor.fetchone()[0]
        if count > 0:
            remaining_refs += count
            print(f"⚠️  {count} rows still have special guild ID in table '{table}'")
    
    # Check for new personal guild ID references
    personal_refs = 0
    
    for table in tables_to_check:
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name=?
        """, (table,))
        
        if not cursor.fetchone():
            continue
            
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'guild_id' not in columns:
            continue
            
        cursor.execute(f"""
            SELECT COUNT(*) FROM {table} 
            WHERE guild_id = ?
        """, (PERSONAL_GUILD_ID,))
        
        count = cursor.fetchone()[0]
        if count > 0:
            personal_refs += count
            print(f"✅ {count} rows now have personal guild ID in table '{table}'")
    
    return remaining_refs, personal_refs

def main():
    """Main function to execute the guild ID update."""
    if not DB_PATH.exists():
        print(f"❌ Database file not found: {DB_PATH}")
        print("Make sure you're running this script from the DraftBot directory.")
        sys.exit(1)
    
    print("🔄 Starting guild ID update process...")
    print(f"From: {SPECIAL_GUILD_ID}")
    print(f"To:   {PERSONAL_GUILD_ID}")
    print()
    
    # Create backup
    backup_path = backup_database()
    
    try:
        # Connect to database
        with sqlite3.connect(DB_PATH) as conn:
            # Begin transaction
            conn.execute("BEGIN TRANSACTION")
            
            print("🔄 Updating guild ID references...")
            total_updates = update_guild_references(conn)
            
            print(f"\n📊 Total rows updated: {total_updates}")
            
            # Verify updates
            print("\n🔍 Verifying updates...")
            remaining_refs, personal_refs = verify_updates(conn)
            
            if remaining_refs > 0:
                print(f"\n⚠️  Warning: {remaining_refs} rows still have the special guild ID")
                response = input("Continue anyway? (y/N): ")
                if response.lower() != 'y':
                    conn.execute("ROLLBACK")
                    print("❌ Update cancelled")
                    return
            
            # Commit transaction
            conn.execute("COMMIT")
            print(f"\n✅ Successfully updated guild ID references!")
            print(f"✅ {personal_refs} rows now reference your personal guild ID")
            
    except Exception as e:
        print(f"\n❌ Error during update: {e}")
        print(f"💾 Database backup available at: {backup_path}")
        sys.exit(1)

if __name__ == "__main__":
    main()