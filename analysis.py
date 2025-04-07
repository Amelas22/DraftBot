#!/usr/bin/env python3
"""
Min vs Max Team Analysis Script

This script analyzes the outcomes of staked drafts to determine whether the min team 
(team with lower total stake) or max team (team with higher total stake) wins more often.

Usage:
    python analysis.py

Requirements:
    - SQLAlchemy
    - pandas (required for analysis)
"""

import asyncio
from typing import Dict, List, Tuple, Optional, Any
import logging
from pathlib import Path
from datetime import datetime

# SQLAlchemy imports
from sqlalchemy import select, and_, or_, func

# Direct imports from project modules
from database.db_session import db_session
from models import DraftSession, StakeInfo, MatchResult

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("stake_analysis.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Required imports for analysis
try:
    import pandas as pd
    import numpy as np
    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False
    logger.warning("pandas not available, detailed analysis will be disabled")

class StakeAnalyzer:
    def __init__(self):
        """Initialize the analyzer."""
        # Results storage
        self.results = {
            'total_staked_drafts': 0,
            'valid_drafts': 0,
            'min_team_wins': 0,
            'max_team_wins': 0, 
            'draws': 0,
            'min_team_win_pct': 0,
            'max_team_win_pct': 0,
            'draw_pct': 0,
            'detailed_results': []
        }
    
    async def fetch_staked_drafts(self) -> List[DraftSession]:
        """Fetch all completed draft sessions that have associated stakes and victory messages."""
        async with db_session() as session:
            # Query for draft sessions that have stakes AND a victory message
            query = select(DraftSession).join(
                StakeInfo, DraftSession.session_id == StakeInfo.session_id
            ).where(
                # Filter for drafts with a victory message
                or_(
                    DraftSession.victory_message_id_draft_chat.isnot(None),
                    DraftSession.victory_message_id_results_channel.isnot(None)
                )
            ).distinct()
            
            result = await session.execute(query)
            drafts = result.scalars().all()
            
            logger.info(f"Found {len(drafts)} completed draft sessions with stakes and victory messages")
            return drafts
    
    async def fetch_stakes_for_draft(self, session_id: str) -> List[StakeInfo]:
        """Fetch all stakes for a specific draft session."""
        async with db_session() as session:
            query = select(StakeInfo).filter(StakeInfo.session_id == session_id)
            result = await session.execute(query)
            stakes = result.scalars().all()
            return stakes
    
    async def fetch_match_results_for_draft(self, session_id: str) -> List[MatchResult]:
        """Fetch all match results for a specific draft session."""
        async with db_session() as session:
            query = select(MatchResult).filter(MatchResult.session_id == session_id)
            result = await session.execute(query)
            matches = result.scalars().all()
            return matches
    
    def determine_min_max_teams(self, draft: DraftSession, stakes: List[StakeInfo]) -> Tuple[str, str, float, float]:
        """Determine which team is the min team and which is the max team based on total stakes."""
        # Get team members
        team_a = draft.team_a if isinstance(draft.team_a, list) else []
        team_b = draft.team_b if isinstance(draft.team_b, list) else []
        
        # Calculate total stakes for each team
        team_a_stakes = sum(stake.max_stake for stake in stakes if stake.player_id in team_a)
        team_b_stakes = sum(stake.max_stake for stake in stakes if stake.player_id in team_b)
        
        # Determine min and max teams
        if team_a_stakes <= team_b_stakes:
            return 'A', 'B', team_a_stakes, team_b_stakes
        else:
            return 'B', 'A', team_b_stakes, team_a_stakes
    
    def determine_winner(self, draft: DraftSession, match_results: List[MatchResult]) -> Optional[str]:
        """Determine the winner of a draft based on victory message and match results."""
        # Get team members
        team_a = draft.team_a if isinstance(draft.team_a, list) else []
        team_b = draft.team_b if isinstance(draft.team_b, list) else []
        
        # Verify this draft has a victory message (should be filtered already, but double-check)
        has_victory_message = (
            hasattr(draft, 'victory_message_id_draft_chat') and draft.victory_message_id_draft_chat or
            hasattr(draft, 'victory_message_id_results_channel') and draft.victory_message_id_results_channel
        )
        
        if not has_victory_message:
            logger.warning(f"Draft {draft.session_id} does not have a victory message, skipping")
            return None
            
        # If draft has a direct winner field, use that
        if hasattr(draft, 'winner_team') and draft.winner_team:
            return draft.winner_team
        
        # Otherwise, count match wins for each team
        team_a_wins = 0
        team_b_wins = 0
        
        for match in match_results:
            if not match.winner_id:
                continue
                
            # Determine which team the winner belongs to
            if match.winner_id in team_a:
                team_a_wins += 1
            elif match.winner_id in team_b:
                team_b_wins += 1
        
        # Determine winner based on match count
        if team_a_wins > team_b_wins:
            return 'A'
        elif team_b_wins > team_a_wins:
            return 'B'
        else:
            # Equal or no matches found
            return None
    
    async def analyze_draft(self, draft: DraftSession) -> Dict[str, Any]:
        """Analyze a single draft to determine if min or max team won."""
        # Verify this draft has a victory message
        has_victory_message = (
            hasattr(draft, 'victory_message_id_draft_chat') and draft.victory_message_id_draft_chat or
            hasattr(draft, 'victory_message_id_results_channel') and draft.victory_message_id_results_channel
        )
        
        if not has_victory_message:
            logger.warning(f"Draft {draft.session_id} does not have a victory message, skipping")
            return None
            
        stakes = await self.fetch_stakes_for_draft(draft.session_id)
        if not stakes:
            logger.warning(f"Draft {draft.session_id} has no stakes, skipping")
            return None
        
        # Get team assignments
        team_a = draft.team_a if isinstance(draft.team_a, list) else []
        team_b = draft.team_b if isinstance(draft.team_b, list) else []
        
        if not team_a or not team_b:
            logger.warning(f"Draft {draft.session_id} has incomplete team data, skipping")
            return None
        
        # Determine min and max teams
        min_team, max_team, min_team_stakes, max_team_stakes = self.determine_min_max_teams(draft, stakes)
        
        # Get match results and determine winner
        match_results = await self.fetch_match_results_for_draft(draft.session_id)
        winner = self.determine_winner(draft, match_results)
        
        result = {
            'session_id': draft.session_id,
            'draft_id': draft.draft_id if hasattr(draft, 'draft_id') else None,
            'min_team': min_team,
            'max_team': max_team,
            'min_team_stakes': min_team_stakes,
            'max_team_stakes': max_team_stakes,
            'stake_ratio': min_team_stakes / max_team_stakes if max_team_stakes else 0,
            'winner': winner,
            'outcome': None,
            'date': draft.draft_start_time if hasattr(draft, 'draft_start_time') else None
        }
        
        # Record the outcome
        if winner:
            if winner == min_team:
                result['outcome'] = 'min_win'
            elif winner == max_team:
                result['outcome'] = 'max_win'
        else:
            result['outcome'] = 'draw'
        
        logger.info(f"Draft {draft.session_id} - Min Team: {min_team} ({min_team_stakes}), "
                   f"Max Team: {max_team} ({max_team_stakes}), Winner: {winner}")
        
        return result
    
    async def analyze_all_drafts(self):
        """Analyze all staked drafts in the database."""
        drafts = await self.fetch_staked_drafts()
        valid_results = []
        
        for draft in drafts:
            result = await self.analyze_draft(draft)
            if result:
                valid_results.append(result)
        
        # Tabulate results
        self.results['total_staked_drafts'] = len(drafts)
        self.results['valid_drafts'] = len(valid_results)
        self.results['detailed_results'] = valid_results
        
        # Count outcomes
        for result in valid_results:
            if result['outcome'] == 'min_win':
                self.results['min_team_wins'] += 1
            elif result['outcome'] == 'max_win':
                self.results['max_team_wins'] += 1
            elif result['outcome'] == 'draw':
                self.results['draws'] += 1
        
        # Calculate percentages
        if self.results['valid_drafts'] > 0:
            self.results['min_team_win_pct'] = (self.results['min_team_wins'] / self.results['valid_drafts']) * 100
            self.results['max_team_win_pct'] = (self.results['max_team_wins'] / self.results['valid_drafts']) * 100
            self.results['draw_pct'] = (self.results['draws'] / self.results['valid_drafts']) * 100
        
        return self.results
    
    def print_summary(self):
        """Print a summary of the analysis results."""
        print("\n===== STAKE ANALYSIS SUMMARY =====")
        print(f"Total completed staked drafts analyzed: {self.results['total_staked_drafts']}")
        print(f"Valid drafts with victory messages: {self.results['valid_drafts']}")
        print("\nOverall Outcomes:")
        print(f"Min team wins: {self.results['min_team_wins']} ({self.results['min_team_win_pct']:.2f}%)")
        print(f"Max team wins: {self.results['max_team_wins']} ({self.results['max_team_win_pct']:.2f}%)")
        print(f"Draws: {self.results['draws']} ({self.results['draw_pct']:.2f}%)")
        
        # Print ratio-based analysis if pandas is available
        if ANALYSIS_AVAILABLE and self.results['detailed_results']:
            self.analyze_distribution()
            self.create_five_equal_buckets()

    def analyze_distribution(self):
        """Analyze the distribution of stake ratios and suggest optimal bins."""
        if not ANALYSIS_AVAILABLE or not self.results['detailed_results']:
            logger.warning("Cannot analyze distribution - pandas not available or no results")
            return
            
        print("\n===== STAKE RATIO DISTRIBUTION ANALYSIS =====")
        
        df = pd.DataFrame(self.results['detailed_results'])
        
        # Basic statistics
        min_ratio = df['stake_ratio'].min()
        max_ratio = df['stake_ratio'].max()
        mean_ratio = df['stake_ratio'].mean()
        median_ratio = df['stake_ratio'].median()
        
        print(f"Min ratio: {min_ratio:.2f}")
        print(f"Max ratio: {max_ratio:.2f}")
        print(f"Mean ratio: {mean_ratio:.2f}")
        print(f"Median ratio: {median_ratio:.2f}")
    
    def create_five_equal_buckets(self):
        """Create 5 equal-sized buckets based on the number of drafts."""
        if not ANALYSIS_AVAILABLE or not self.results['detailed_results']:
            return
        
        print("\n===== OUTCOMES BY STAKE RATIO (5 EQUAL-SIZED BUCKETS) =====")
        print("(Drafts divided into 5 equal-sized groups by stake ratio)")
        
        # Create DataFrame and sort by stake ratio
        df = pd.DataFrame(self.results['detailed_results'])
        df = df.sort_values('stake_ratio')
        
        # Count total drafts
        total_drafts = len(df)
        
        # Calculate drafts per bucket (approx. 20% each)
        drafts_per_bucket = total_drafts // 5
        remainder = total_drafts % 5
        
        # Create bucket size list (distribute remainder across buckets)
        bucket_sizes = [drafts_per_bucket + (1 if i < remainder else 0) for i in range(5)]
        
        # Create bucket labels
        bucket_labels = [f"Bucket {i+1}" for i in range(5)]
        
        # Add bucket column to dataframe
        df['bucket'] = None
        
        # Assign buckets based on equal number of drafts
        start_idx = 0
        for i, size in enumerate(bucket_sizes):
            end_idx = start_idx + size
            bucket_indices = df.index[start_idx:end_idx]
            df.loc[bucket_indices, 'bucket'] = i
            start_idx = end_idx
        
        # Get bucket ranges
        bucket_ranges = []
        for i in range(5):
            bucket_df = df[df['bucket'] == i]
            if len(bucket_df) > 0:
                min_ratio = bucket_df['stake_ratio'].min()
                max_ratio = bucket_df['stake_ratio'].max()
                bucket_ranges.append((min_ratio, max_ratio))
                print(f"Bucket {i+1}: {min_ratio:.2f}-{max_ratio:.2f}, {len(bucket_df)} drafts ({len(bucket_df)/total_drafts*100:.1f}%)")
            else:
                bucket_ranges.append((0, 0))
        
        # Group by bucket and outcome
        grouped = df.groupby(['bucket', 'outcome']).size().unstack(fill_value=0)
        
        # Calculate totals
        grouped['total'] = grouped.sum(axis=1)
        
        # Calculate percentages
        pct_df = pd.DataFrame()
        for col in ['min_win', 'max_win', 'draw']:
            if col in grouped.columns:
                pct_df[f'{col}_pct'] = (grouped[col] / grouped['total'] * 100).round(1)
        
        # Combine with counts
        result_df = pd.concat([grouped, pct_df], axis=1)
        
        # Print table header
        print("\nBucket       | Ratio Range   | Total Drafts | Min Team Wins  | Max Team Wins  | Draws")
        print("-------------|---------------|--------------|----------------|----------------|-------")
        
        # Print each row
        for bucket in range(5):
            if bucket in result_df.index:
                row = result_df.loc[bucket]
                
                min_ratio, max_ratio = bucket_ranges[bucket]
                ratio_range = f"{min_ratio:.2f}-{max_ratio:.2f}"
                
                total_drafts = row['total'] if 'total' in row else 0
                
                min_wins = row['min_win'] if 'min_win' in row else 0
                min_pct = row['min_win_pct'] if 'min_win_pct' in row else 0
                
                max_wins = row['max_win'] if 'max_win' in row else 0
                max_pct = row['max_win_pct'] if 'max_win_pct' in row else 0
                
                draws = row['draw'] if 'draw' in row else 0
                draw_pct = row['draw_pct'] if 'draw_pct' in row else 0
                
                print(f"Bucket {bucket+1}     | {ratio_range:13} | {int(total_drafts):12d} | {int(min_wins):4d} ({min_pct:5.1f}%) | {int(max_wins):4d} ({max_pct:5.1f}%) | {int(draws):2d} ({draw_pct:5.1f}%)")
        
        # Print overall row
        min_total = self.results['min_team_wins']
        min_pct = self.results['min_team_win_pct']
        max_total = self.results['max_team_wins'] 
        max_pct = self.results['max_team_win_pct']
        draw_total = self.results['draws']
        draw_pct = self.results['draw_pct']
        total_drafts = self.results['valid_drafts']
        
        print("-------------|---------------|--------------|----------------|----------------|-------")
        print(f"OVERALL       | {df['stake_ratio'].min():.2f}-{df['stake_ratio'].max():.2f}     | {int(total_drafts):12d} | {int(min_total):4d} ({min_pct:5.1f}%) | {int(max_total):4d} ({max_pct:5.1f}%) | {int(draw_total):2d} ({draw_pct:5.1f}%)")
    
    def export_detailed_results(self):
        """Export detailed results to a CSV file."""
        if not self.results['detailed_results']:
            logger.warning("No detailed results to export")
            return
            
        try:
            import pandas as pd
            df = pd.DataFrame(self.results['detailed_results'])
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"stake_analysis_detailed_{timestamp}.csv"
            
            df.to_csv(filename, index=False)
            logger.info(f"Detailed results exported to {filename}")
            
        except ImportError:
            logger.warning("pandas not available, skipping detailed export")
            
            # Fallback to basic CSV export
            import csv
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"stake_analysis_detailed_{timestamp}.csv"
            
            with open(filename, 'w', newline='') as csvfile:
                if self.results['detailed_results']:
                    fieldnames = self.results['detailed_results'][0].keys()
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(self.results['detailed_results'])
                    
            logger.info(f"Detailed results exported to {filename}")

async def main():
    """Main function to run the analysis."""
    try:
        analyzer = StakeAnalyzer()
        
        logger.info("Starting stake analysis...")
        await analyzer.analyze_all_drafts()
        
        analyzer.print_summary()
        analyzer.export_detailed_results()  # Still export the CSV but skip visualizations
        
        logger.info("Analysis complete!")
        
    except Exception as e:
        logger.error(f"Error during analysis: {e}", exc_info=True)
        
if __name__ == "__main__":
    asyncio.run(main())