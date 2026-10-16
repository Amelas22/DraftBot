"""A draft is a donation draft only when it was opened as one."""
import pytest

from conftest import seed_session
from session import get_draft_session


@pytest.mark.asyncio
async def test_a_draft_is_not_a_donation_draft_unless_opened_as_one(test_db):
    await seed_session("s1", stage=None)
    await seed_session("s2", stage=None, donation=True)

    assert (await get_draft_session("s1")).donation is False
    assert (await get_draft_session("s2")).donation is True
