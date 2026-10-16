import pytest

from preference_service import (get_anonymous_donors, get_player_dm_notification_preference,
                                get_uses_library, set_donation_anonymous)


@pytest.mark.asyncio
async def test_a_donor_who_opts_out_is_anonymous_until_they_opt_back_in(test_db):
    await set_donation_anonymous("p1", "g", True)
    assert await get_anonymous_donors(["p1", "p2"], "g") == {"p1"}

    await set_donation_anonymous("p1", "g", False)
    assert await get_anonymous_donors(["p1"], "g") == set()


@pytest.mark.asyncio
async def test_answering_it_decides_no_other_preference(test_db):
    await set_donation_anonymous("p1", "g", True)

    assert await get_uses_library("p1", "g") is None
    assert await get_player_dm_notification_preference("p1", "g") == \
        await get_player_dm_notification_preference("p2", "g")
