from datetime import datetime, timezone

import pytest

from conftest import test_db  # noqa: F401
from database.db_session import db_session
from services.team_draft import (
    Picks,
    by_thread,
    check_sides,
    claim_draft,
    create_team_draft,
    release_draft,
    set_room,
)

GUILD = "900"
ROLES = {"1": ["10", "11"], "2": ["20", "21"], "3": ["10", "30"], "4": None, "5": [str(100 + i) for i in range(13)], "6": [str(100 + i) for i in range(12)]}


def _members(role_id):
    return ROLES.get(role_id, [])


def _not_bot(_user_id):
    return False


def _check(a, b, members=_members, is_bot=_not_bot):
    return check_sides(a, b, members, GUILD, is_bot)


def test_check_sides_accepts_two_clean_sides():
    assert _check(Picks(("1",), ()), Picks(("2",), ("50",))) is None


def test_check_sides_refuses_an_empty_side():
    text = _check(Picks(("1",), ()), Picks((), ()))
    assert text is not None and "at least one" in text


def test_check_sides_refuses_everyone():
    text = _check(Picks((GUILD,), ()), Picks(("2",), ()))
    assert text is not None and "@everyone" in text


def test_check_sides_refuses_the_same_pick_twice():
    assert _check(Picks(("1",), ()), Picks(("1",), ())) is not None
    assert _check(Picks((), ("50",)), Picks(("2",), ("50",))) is not None


def test_check_sides_refuses_a_person_on_both_sides_after_roles_expand():
    text = _check(Picks(("1",), ()), Picks(("3",), ()))
    assert text is not None and "1 person" in text
    text = _check(Picks(("1",), ("20",)), Picks(("3", "2"), ()))
    assert text is not None and "2 people" in text


def test_check_sides_refuses_a_side_over_twelve():
    text = _check(Picks(("5",), ()), Picks(("2",), ()))
    assert text is not None and "12" in text


def test_check_sides_allows_exactly_twelve():
    assert _check(Picks(("6",), ()), Picks(("2",), ())) is None


def test_check_sides_refuses_more_than_five_picks():
    many = Picks(("1",), ("50", "51", "52", "53", "54"))
    text = _check(many, Picks(("2",), ()))
    assert text is not None and "5" in text


def test_check_sides_ignores_bots_when_counting():
    # Roles 1 and 3 share member 10: a clash, unless 10 is a bot.
    assert _check(Picks(("1",), ()), Picks(("3",), ())) is not None
    assert _check(Picks(("1",), ()), Picks(("3",), ()), is_bot=lambda u: u == "10") is None
    # A side made only of bots is empty.
    text = _check(Picks(("1",), ()), Picks(("2",), ()), is_bot=lambda u: u in {"20", "21"})
    assert text is not None and "at least one" in text
    # Thirteen members, one a bot, fits under the limit.
    assert _check(Picks(("5",), ()), Picks(("2",), ()), is_bot=lambda u: u == "100") is None


def test_a_deleted_role_counts_as_nobody():
    assert _check(Picks(("4",), ("50",)), Picks(("2",), ())) is None
    text = _check(Picks(("4",), ()), Picks(("2",), ()))
    assert text is not None and "at least one" in text


async def _make(s, **kw):
    args = dict(guild_id=GUILD, channel_id="c", name="A vs B", a=Picks(("1",), ()),
                b=Picks(("2",), ("50",)), created_by="9", now=datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
    args.update(kw)
    return await create_team_draft(s, **args)


@pytest.mark.asyncio
async def test_create_stores_the_sides(test_db):  # noqa: F811
    async with db_session() as s:
        td = await _make(s)
        await s.commit()
        assert td.id is not None
        assert (td.side_a_roles, td.side_b_roles, td.side_b_users) == (["1"], ["2"], ["50"])
        assert td.created_utc == datetime(2026, 10, 26, 12)


@pytest.mark.asyncio
async def test_only_the_first_claim_wins(test_db):  # noqa: F811
    async with db_session() as s:
        td = await _make(s)
        await s.commit()
        assert await claim_draft(s, td.id, "d1") is True
        assert await claim_draft(s, td.id, "d2") is False
        await release_draft(s, td.id, "wrong")
        assert await claim_draft(s, td.id, "d3") is False
        await release_draft(s, td.id, "d1")
        assert await claim_draft(s, td.id, "d4") is True


@pytest.mark.asyncio
async def test_by_thread_finds_the_matchup(test_db):  # noqa: F811
    async with db_session() as s:
        td = await _make(s)
        await set_room(s, td.id, "t1", "m1")
        await s.commit()
        got = await by_thread(s, "t1")
        assert got is not None and got.id == td.id and got.control_message_id == "m1"
        assert await by_thread(s, "nope") is None
