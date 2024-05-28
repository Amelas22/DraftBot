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
    "216594497147109376": {"display_name": "Rootofpie", "win_points": 1},
    "190182896986882049": {"display_name": "RedDog", "win_points": 0},
    "241050564887183360": {"display_name": "BroccoliRob", "win_points": 2},
    "402977837088505889": {"display_name": "admiral_ace", "win_points": 2},
    "733928659001278514": {"display_name": "taylorbehrens", "win_points": 1},
    "322475575522361355": {"display_name": "Durotan97", "win_points": 3},
    "286797275496316929": {"display_name": "mgoat", "win_points": 1},
    "117223984054927365": {"display_name": "Zrifts", "win_points": 2}
}
session_id = "241050564887183360-1716914676"

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
                    check = player_weekly_limit.drafts_participated
                    if check == 1:
                        player_weekly_limit.match_one_points = player['win_points']
                    elif check == 2:
                        player_weekly_limit.match_two_points = player['win_points']
                    elif check == 3:
                        player_weekly_limit.match_three_points = player['win_points']
                    elif check > 3:
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