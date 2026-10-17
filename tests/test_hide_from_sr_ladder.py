import pytest

from preference_service import (get_anonymous_donors, get_hide_from_sr_ladder,
                                get_player_dm_notification_preference,
                                get_uses_library, set_hide_from_sr_ladder)


@pytest.mark.asyncio
async def test_hiding_round_trips(test_db):
    assert await get_hide_from_sr_ladder("p1", "g") is False
    assert await set_hide_from_sr_ladder("p1", "g", True) is True
    assert await get_hide_from_sr_ladder("p1", "g") is True
    await set_hide_from_sr_ladder("p1", "g", False)
    assert await get_hide_from_sr_ladder("p1", "g") is False


@pytest.mark.asyncio
async def test_answering_it_decides_no_other_preference(test_db):
    await set_hide_from_sr_ladder("p1", "g", True)
    assert await get_uses_library("p1", "g") is None
    assert await get_anonymous_donors(["p1"], "g") == set()
    assert await get_player_dm_notification_preference("p1", "g") == \
        await get_player_dm_notification_preference("p2", "g")
