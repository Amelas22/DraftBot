#!/usr/bin/env python3
"""
Script to retrigger log posting for drafts that haven't had their logs posted
in the last week. This script finds drafts where logs_message_id is NULL
and attempts to repost them using the existing infrastructure.
"""

import asyncio
import datetime
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker
from models import DraftSession
from datacollections import DataCollections
import discord
from config import DISCORD_BOT_TOKEN, SPECIAL_GUILD_ID
import json
import sys

class LogRetrigger:
    def __init__(self):
        self.engine = create_engine('sqlite:///drafts.db')
        self.Session = sessionmaker(bind=self.engine)
        self.client = None
        
    async def init_discord(self):
        """Initialize Discord client"""
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        await self.client.login(DISCORD_BOT_TOKEN)
        
    async def find_unposted_drafts(self, days_back=7):
        """Find drafts from the last N days that don't have logs posted"""
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days_back)
        
        session = self.Session()
        try:
            unposted_drafts = session.query(DraftSession).filter(
                and_(
                    DraftSession.draft_start_time >= cutoff_date,
                    DraftSession.logs_message_id.is_(None),
                    DraftSession.data_received == True,
                    DraftSession.session_stage == "COMPLETED"
                )
            ).order_by(DraftSession.draft_start_time.desc()).all()
            
            return unposted_drafts
        finally:
            session.close()
            
    async def find_draft_logs_channel(self, guild_id):
        """Find the draft-logs channel in the guild"""
        guild = self.client.get_guild(int(guild_id))
        if not guild:
            print(f"Could not find guild {guild_id}")
            return None
            
        for channel in guild.channels:
            if channel.name.lower() == "draft-logs" and hasattr(channel, "send"):
                return channel
        return None
        
    async def retrigger_logs_for_draft(self, draft_session, dry_run=True):
        """Attempt to retrigger log posting for a single draft"""
        print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing Draft {draft_session.id}")
        print(f"  Session ID: {draft_session.session_id}")
        print(f"  Date: {draft_session.draft_start_time}")
        print(f"  Guild: {draft_session.guild_id}")
        print(f"  Cube: {draft_session.cube}")
        
        if not draft_session.guild_id:
            print(f"  ❌ No guild_id set")
            return False
            
        # Find the draft logs channel
        logs_channel = await self.find_draft_logs_channel(draft_session.guild_id)
        if not logs_channel:
            print(f"  ❌ No 'draft-logs' channel found in guild")
            return False
            
        print(f"  📝 Found logs channel: #{logs_channel.name}")
        
        # Check if we have MagicProTools links
        if draft_session.magicprotools_links:
            try:
                links = json.loads(draft_session.magicprotools_links) if isinstance(draft_session.magicprotools_links, str) else draft_session.magicprotools_links
                print(f"  🔗 Has {len(links)} MagicProTools links")
                
                if not dry_run:
                    # Use existing DataCollections infrastructure to generate and post embed
                    dc = DataCollections()
                    
                    # Generate the embed using existing method
                    embed = await dc.generate_magicprotools_embed(draft_session, links)
                    
                    # Post the embed
                    message = await logs_channel.send(embed=embed)
                    
                    # Update the database
                    session = self.Session()
                    try:
                        db_draft = session.query(DraftSession).filter_by(id=draft_session.id).first()
                        db_draft.logs_message_id = str(message.id)
                        db_draft.logs_channel_id = str(logs_channel.id)
                        session.commit()
                        print(f"  ✅ Posted logs and updated database")
                        return True
                    except Exception as e:
                        session.rollback()
                        print(f"  ❌ Database update failed: {e}")
                        return False
                    finally:
                        session.close()
                else:
                    print(f"  ✅ Would post logs (dry run)")
                    return True
                    
            except Exception as e:
                print(f"  ❌ Error processing MagicProTools links: {e}")
                return False
        else:
            print(f"  ❌ No MagicProTools links available")
            return False
            
    async def retrigger_all_unposted(self, days_back=7, dry_run=True):
        """Find and retrigger all unposted logs from the last N days"""
        print(f"{'🧪 DRY RUN MODE - No changes will be made' if dry_run else '🚀 LIVE MODE - Changes will be applied'}")
        print(f"Searching for unposted draft logs from the last {days_back} days...")
        
        await self.init_discord()
        
        try:
            unposted_drafts = await self.find_unposted_drafts(days_back)
            print(f"Found {len(unposted_drafts)} drafts with unposted logs")
            
            if len(unposted_drafts) == 0:
                print("✅ All recent drafts already have logs posted!")
                return
            
            success_count = 0
            
            for draft in unposted_drafts:
                try:
                    success = await self.retrigger_logs_for_draft(draft, dry_run)
                    if success:
                        success_count += 1
                except Exception as e:
                    print(f"  ❌ Error processing draft {draft.id}: {e}")
                    
            print(f"\n📊 Summary:")
            print(f"  Total drafts processed: {len(unposted_drafts)}")
            print(f"  Successfully {'would be ' if dry_run else ''}processed: {success_count}")
            print(f"  Failed: {len(unposted_drafts) - success_count}")
            
        finally:
            await self.client.close()

async def main():
    if len(sys.argv) < 2:
        print("Usage: pipenv run python retrigger_unposted_logs.py <days_back> [--live]")
        print("  days_back: Number of days back to search (default: 7)")
        print("  --live: Actually post the logs (default: dry run)")
        print("Examples:")
        print("  pipenv run python retrigger_unposted_logs.py 7        # Dry run for last 7 days")
        print("  pipenv run python retrigger_unposted_logs.py 14 --live # Actually post logs for last 14 days")
        return
        
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    dry_run = "--live" not in sys.argv
    
    retrigger = LogRetrigger()
    await retrigger.retrigger_all_unposted(days_back, dry_run)

if __name__ == "__main__":
    asyncio.run(main())