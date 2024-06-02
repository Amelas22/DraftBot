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
    "642857806822637589": {"display_name": "lowghost1", "drafts_participated": 3},
}
# session_id = "440858038669410305-1716432833"

async def fix_results(players, session_id=None):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            draft_session = await get_draft_session(session_id)
            pacific = pytz.timezone('US/Pacific')
            utc = pytz.utc
            now = datetime.now()
            pacific_time = utc.localize(now).astimezone(pacific)
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
                    player_weekly_limit.drafts_participated = player['drafts_participated']
                    print("Player Limit Updated")

                    await session.commit()

    print(player_weekly_limit.drafts_participated, player_weekly_limit.match_one_points, player_weekly_limit.match_two_points, player_weekly_limit.match_three_points, player_weekly_limit.match_four_points, player_weekly_limit.WeekStartDate)
# Run the script
if __name__ == "__main__":
    asyncio.run(fix_results(players))