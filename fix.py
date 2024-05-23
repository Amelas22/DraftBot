import asyncio
import pytz
from sqlalchemy.future import select
from session import AsyncSessionLocal, PlayerLimit, get_draft_session
from datetime import datetime, timedelta

'''
input sign_up dictionary and session_id for affected draft here with win_points added
example:
players[user_id] = {
            "display_name": display_name,
            "win_points": 0
            }
'''
players = {
    "440858038669410305": {"display_name": "Blake (EvoPride)", "win_points": 2},
    "212406460322283521": {"display_name": "GPJedi", "win_points": 1},
    "216054618403110913": {"display_name": "Njainchill (Firegothim1234)", "win_points": 0},
    "128600166868582403": {"display_name": "ZWatty", "win_points": 1},
    "504000365843316763": {"display_name": "harmonywoods", "win_points": 2},
    "201538181382930432": {"display_name": "JewPCabra666 (Pwnergod411)", "win_points": 2},
    "106933537659174912": {"display_name": "Muhara", "win_points": 2},
    "706231900171403324": {"display_name": "shallot", "win_points": 3}
}
session_id = "440858038669410305-1716432833"

async def fix_results(players, session_id):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            draft_session = await get_draft_session(session_id)
            pacific = pytz.timezone('US/Pacific')
            utc = pytz.utc
            pacific_time = utc.localize(draft_session.teams_start_time).astimezone(pacific)
            midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))
            start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
            for user_id, player in players.items():
                # Query to find existing entry for the player this week
                player_weekly_limit_stmt = select(PlayerLimit).where(
                    PlayerLimit.player_id == user_id,
                    PlayerLimit.WeekStartDate == start_of_week
                )
                player_weekly_limit_result = await session.execute(player_weekly_limit_stmt)
                player_weekly_limit = player_weekly_limit_result.scalars().first()

                if player_weekly_limit:
                    player_weekly_limit.drafts_participated += 1
                    if player_weekly_limit.drafts_participated == 1:
                        player_weekly_limit.match_one_points = player['win_points']
                    elif player_weekly_limit.drafts_participated == 2:
                        player_weekly_limit.match_two_points = player['win_points']
                    elif player_weekly_limit.drafts_participated == 3:
                        player_weekly_limit.match_three_points = player['win_points']
                    elif player_weekly_limit.drafts_participated > 3:
                        player_weekly_limit.match_four_points = player['win_points']
                else:
                    # If not, create a new record
                    new_player_limit = PlayerLimit(
                        player_id=user_id,
                        display_name=player['display_name'],
                        drafts_participated=1,
                        WeekStartDate=start_of_week,
                        match_one_points=player['win_points'],
                        match_two_points=0,
                        match_three_points=0,
                        match_four_points=0
                    )
                    session.add(new_player_limit)

# Run the script
if __name__ == "__main__":
    asyncio.run(fix_results(players, session_id))