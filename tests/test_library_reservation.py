"""A rental library's copy of a cube, reserved by the queue that will use it.

The first eligible borrower to sign up to a filling queue reserves the copy if
the shelf can cover it. While that queue holds it, the copy is in use: a second
queue of the same cube is told where it is. Each borrower agrees before the
draft fires; once teams form, only those who agreed need it, so a copy nobody
agreed to goes back then. The reservation lasts until nobody
in the draft needs it -- they left, released it, returned their deck, or played
a match without one -- or the draft is decided, over, or its queue went quiet.
Released is for good.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.match import MatchResult
from models.player import PlayerStats
import preference_service as prefs
import services.card_library_inventory as inv
import services.library_reservation as resv

pytestmark = pytest.mark.asyncio

LIB, GUILD, CUBE = "lounge", "g1", "mycube"
ANN, BEN, CAT = "1001", "1002", "1003"
DECK = [{"name": "Swamp", "qty": 4}]


@pytest_asyncio.fixture
async def lounge(test_db, monkeypatch):
    """A rental library with ONE copy of the cube on its shelf."""
    async def fetch(cube_id):
        return list(DECK) if cube_id == CUBE else None
    monkeypatch.setattr(inv, "fetch_cube", fetch)
    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    collateral_untrusted=100, cubes=(CUBE,), min_drafts=3,
                    stock={"Swamp": 4})


async def _borrower(*players):
    """Experienced, and saved as borrowing."""
    async with AsyncSessionLocal() as s:
        for p in players:
            s.add(PlayerStats(player_id=p, guild_id=GUILD, display_name=p,
                              drafts_participated=5))
        await s.commit()
    for p in players:
        await prefs.set_uses_library(p, GUILD, True)


async def _queue(sid, players, *, fired=False, quiet=False):
    now = datetime.now()
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=sid, guild_id=GUILD, cube=CUBE,
            sign_ups={p: p for p in players}, draft_start_time=now,
            deletion_time=now + (timedelta(minutes=-1) if quiet else timedelta(hours=3)),
            draft_channel_id="chan", message_id=f"msg-{sid}",
            teams_start_time=now if fired else None))
        await s.commit()


async def _row(sid):
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        return (await s.execute(select(DraftSession).where(
            DraftSession.session_id == sid))).scalar_one()


async def _set(sid, **values):
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        row = (await s.execute(select(DraftSession).where(
            DraftSession.session_id == sid))).scalar_one()
        for k, v in values.items():
            setattr(row, k, v)
        await s.commit()


async def _available():
    return await inv.library_available(LIB)


# --- taking it ----------------------------------------------------------------

async def test_the_first_borrower_in_a_queue_reserves_the_copy(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])

    assert (await resv.reserve("q1")).status == "reserved"
    assert await resv.still_reserved(await _row("q1"))
    assert await _available() == {}, "the whole cube is held while it fills"


async def test_a_second_borrower_in_the_same_queue_shares_it(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.reserve("q1")

    assert (await resv.reserve("q1")).status == "held"


async def test_a_second_queue_is_told_which_queue_is_using_it(lounge):
    """Not outranked: the copy is simply in use, and they can go and join it."""
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])
    await resv.reserve("q1")

    answer = await resv.reserve("q2")

    assert answer.status == "in_use" and answer.by.session_id == "q1"
    assert resv.queue_link(answer.by).endswith("/chan/msg-q1")


async def test_a_draft_that_fired_without_it_cannot_reserve(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN], fired=True)

    assert (await resv.reserve("q1")).status == "fired"


# --- agreeing -----------------------------------------------------------------

async def test_agreeing_reserves_it_and_records_who_agreed(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])

    assert (await resv.agree_player("q1", ANN)).status == "reserved"
    assert (await _row("q1")).library_agreed == [ANN]


async def test_a_held_copy_nobody_agreed_to_goes_back_when_the_draft_fires(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    assert await resv.owes_agreement(await _row("q1"), ANN)

    await _set("q1", teams_start_time=datetime.now())

    assert not await resv.still_reserved(await _row("q1"))
    assert await _available() == {"Swamp": 4}


async def test_once_it_fires_only_those_who_agreed_need_it(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.reserve("q1")
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now())

    assert await resv.users_if_reserved(await _row("q1")) == {ANN}


async def test_two_players_agreeing_at_once_both_count(lounge):
    """Each reads the list, adds itself and writes it back; unserialised, the
    second write would drop the first player's agreement."""
    import asyncio
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])

    await asyncio.gather(resv.agree_player("q1", ANN), resv.agree_player("q1", BEN))

    assert sorted((await _row("q1")).library_agreed) == [ANN, BEN]


async def test_agreeing_while_another_releases_keeps_both(lounge):
    import asyncio
    await _borrower(ANN, BEN, CAT)
    await _queue("q1", [ANN, BEN, CAT])
    await resv.reserve("q1")
    await resv.release_player("q1", ANN)

    await asyncio.gather(resv.agree_player("q1", ANN), resv.release_player("q1", BEN))

    row = await _row("q1")
    assert row.library_released == [BEN] and row.library_agreed == [ANN]


async def test_agreeing_after_it_fired_is_too_late(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now())

    assert (await resv.agree_player("q1", BEN)).status == "fired"
    assert await resv.users_if_reserved(await _row("q1")) == {ANN}


async def test_agreeing_takes_back_your_own_dont_need_it(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.reserve("q1")
    await resv.release_player("q1", ANN)

    await resv.agree_player("q1", ANN)

    row = await _row("q1")
    assert ANN in await resv.library_users(row) and not row.library_released
    assert not await resv.owes_agreement(row, ANN)


# --- giving it back -----------------------------------------------------------

async def test_the_last_borrower_leaving_releases_it_for_good(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])
    await resv.reserve("q1")
    await _set("q1", sign_ups={"9999": "someone else"})

    assert (await resv.reserve("q2")).status == "reserved"
    assert (await _row("q1")).library_released_at is not None

    await _set("q1", sign_ups={ANN: ANN})
    assert (await resv.reserve("q1")).status == "in_use", \
        "coming back does not take it back from the queue that has it now"


async def test_saying_you_dont_need_it_releases_it_when_you_were_the_last(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.reserve("q1")

    await resv.release_player("q1", ANN)
    assert await resv.still_reserved(await _row("q1")), "Ben still needs it"

    await resv.release_player("q1", BEN)
    assert not await resv.still_reserved(await _row("q1"))
    assert await _available() == {"Swamp": 4}


async def test_playing_a_match_without_a_deck_ends_that_players_need(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now())
    async with AsyncSessionLocal() as s:
        s.add(MatchResult(session_id="q1", match_number=1, player1_id=ANN,
                          player2_id="2002", result_submitted_at=datetime.now()))
        await s.commit()

    assert not await resv.still_reserved(await _row("q1"))


async def test_a_borrower_who_collected_still_needs_it_after_playing(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now())
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=ANN, library_id=LIB,
                       cards=DECK, state="borrowed", source="draft:q1"))
        s.add(MatchResult(session_id="q1", match_number=1, player1_id=ANN,
                          player2_id="2002", result_submitted_at=datetime.now()))
        await s.commit()

    assert await resv.still_reserved(await _row("q1"))


async def test_returning_the_deck_ends_that_players_need(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=ANN, library_id=LIB,
                       cards=DECK, state="returned", source="draft:q1"))
        await s.commit()

    assert not await resv.still_reserved(await _row("q1"))


async def test_a_queue_gone_quiet_releases_it(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    await _set("q1", deletion_time=datetime.now() - timedelta(minutes=1))

    assert not await resv.still_reserved(await _row("q1"))


async def test_switching_the_preference_off_ends_that_players_need(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    await prefs.set_uses_library(ANN, GUILD, False)

    assert not await resv.still_reserved(await _row("q1"))


# --- what it holds ------------------------------------------------------------

async def test_once_decks_exist_only_the_decks_are_held(lounge):
    """Narrowed to what was actually drafted; the rest is free for another pod."""
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now())
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=ANN, library_id=LIB,
                       cards=[{"name": "Swamp", "qty": 1}], state="assigned",
                       source="draft:q1"))
        await s.commit()

    assert await _available() == {"Swamp": 3}


async def test_a_draft_that_fired_without_reserving_holds_nothing(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN], fired=True)

    assert await _available() == {"Swamp": 4}


async def test_the_board_points_a_new_queue_at_the_one_holding_it(lounge):
    from cube_views.pack_options import library_signup_note

    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")

    note = await library_signup_note(CUBE, GUILD)

    assert "in use" in note.lower() and "/chan/msg-q1" in note, note


async def test_a_queue_holding_the_copy_says_so_on_its_own_board(lounge):
    """Its own reservation is the copy it is about to use, not one taken from it:
    the holder's board says the copy is reserved for it, and since when, and
    another queue's board points at the holder."""
    from cube_views.pack_options import library_signup_check

    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])
    assert (await resv.reserve("q1")).status == "reserved"
    since = int((await _row("q1")).library_reserved_at.timestamp())

    own = await library_signup_check(CUBE, GUILD, session_id="q1")
    other = await library_signup_check(CUBE, GUILD, session_id="q2")

    assert own is not None and "reserved for this queue" in own.lower(), own
    assert f"<t:{since}:R>" in own, own
    assert "library available" not in own.lower(), own
    assert "another queue" not in own, own
    assert "in use" in other.lower() and "/chan/msg-q1" in other, other


async def test_a_queue_that_let_the_copy_go_reads_available_again(lounge):
    """Released is for good: the board stops claiming a hold it no longer has."""
    from cube_views.pack_options import library_signup_check

    await _borrower(ANN)
    await _queue("q1", [ANN])
    assert (await resv.reserve("q1")).status == "reserved"
    assert await resv.release_player("q1", ANN)

    own = await library_signup_check(CUBE, GUILD, session_id="q1")

    assert own is not None and "library available" in own.lower(), own
    assert "reserved for this queue" not in own.lower(), own


async def test_a_free_copy_reads_available_before_anyone_reserves_it(lounge):
    from cube_views.pack_options import library_signup_check

    await _queue("q1", [])

    own = await library_signup_check(CUBE, GUILD, session_id="q1")

    assert own is not None and "library available" in own.lower(), own
    assert "reserved for this queue" not in own.lower(), own


async def test_a_long_draft_keeps_the_whole_cube_until_its_decks_exist(lounge):
    """No clock on it: teams to decks can run past an hour, and a reservation
    that quietly stopped holding would let a second queue book the same copy."""
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.agree_player("q1", ANN)
    await _set("q1", teams_start_time=datetime.now() - timedelta(hours=2))

    assert await _available() == {}


async def test_a_stale_release_does_not_undo_a_fresh_reservation(lounge):
    """Released on what it read; a re-reserve in the meantime stands."""
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    stale = await _row("q1")
    stale.sign_ups = {}                    # what the stale read judged
    await _set("q1", library_reserved_at=datetime.now() + timedelta(seconds=1))

    assert await resv.still_reserved(stale) is False
    assert (await _row("q1")).library_released_at is None, \
        "the newer reservation was not the one judged"


async def test_nobody_left_to_need_it_reserves_nothing(lounge):
    await _borrower(ANN)
    await _queue("q1", ["9999"])

    assert (await resv.reserve("q1")).status == "not_applicable"
    assert (await _row("q1")).library_reserved_at is None


# --- every way a fired draft lets it go --------------------------------------

async def _fired_and_agreed(sid="q1"):
    await _borrower(ANN)
    await _queue(sid, [ANN])
    await resv.agree_player(sid, ANN)
    await _set(sid, teams_start_time=datetime.now() - timedelta(hours=3))


@pytest.mark.parametrize("ending", [
    {"session_stage": "abandoned"},
    {"session_stage": "completed"},
    {"victory_message_id_results_channel": "1"},
])
async def test_a_fired_draft_that_ends_gives_the_copy_back(lounge, ending):
    await _fired_and_agreed()
    assert await _available() == {}

    await _set("q1", **ending)

    assert not await resv.still_reserved(await _row("q1"))
    assert await _available() == {"Swamp": 4}


async def test_a_fired_draft_that_fizzled_gives_it_back_once_its_rooms_are_gone(lounge):
    """No log, so no decks, and nobody abandoned it: once cleanup has taken its
    rooms nothing is left to hold the copy for. Without this the library's only
    copy stayed locked until somebody abandoned the draft by hand."""
    await _fired_and_agreed()
    await _set("q1", session_stage="teams",
               deletion_time=datetime.now() - timedelta(days=1))

    assert not await resv.still_reserved(await _row("q1"))
    assert await _available() == {"Swamp": 4}


async def test_a_reaped_draft_with_decks_out_keeps_holding_them(lounge):
    await _fired_and_agreed()
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=ANN, library_id=LIB,
                       cards=[{"name": "Swamp", "qty": 1}], state="borrowed",
                       source="draft:q1"))
        await s.commit()
    await _set("q1", deletion_time=datetime.now() - timedelta(days=1))

    assert await resv.still_reserved(await _row("q1")), "a deck is still out"


# --- a library behind two servers --------------------------------------------

async def test_a_queue_in_another_server_of_the_library_is_told_it_is_in_use(lounge):
    """One shelf, one copy: a regression that scoped reservations by server
    would hand the same copy to both."""
    from models.library_server import LibraryServer
    async with AsyncSessionLocal() as s:
        s.add(LibraryServer(guild_id="g2", library_id=LIB, bound_by="test"))
        s.add(PlayerStats(player_id=BEN, guild_id="g2", display_name=BEN,
                          drafts_participated=5))
        await s.commit()
    await prefs.set_uses_library(BEN, "g2", True)
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    async with AsyncSessionLocal() as s:
        now = datetime.now()
        s.add(DraftSession(session_id="q2", guild_id="g2", cube=CUBE,
                           sign_ups={BEN: BEN}, draft_start_time=now,
                           deletion_time=now + timedelta(hours=3),
                           draft_channel_id="chan2", message_id="msg-q2"))
        await s.commit()

    answer = await resv.reserve("q2")

    assert answer.status == "in_use" and answer.by.session_id == "q1"


# --- the queue changing cube, and the cube that can't be read -----------------

async def test_switching_cube_lets_the_old_copy_go(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    await _set("q1", cube="some-other-cube")

    answer = await resv.cube_changed("q1")

    assert answer.status == "not_applicable", "the library doesn't offer it"
    assert (await _row("q1")).library_released_at is not None
    assert await _available() == {"Swamp": 4}


async def test_switching_to_the_librarys_cube_reserves_it(lounge):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await _set("q1", cube="some-other-cube")
    assert (await resv.reserve("q1")).status == "not_applicable"
    await _set("q1", cube=CUBE)

    assert (await resv.cube_changed("q1")).status == "reserved"


async def test_a_cube_that_cannot_be_read_is_unknown_not_short(lounge, monkeypatch):
    async def unreadable(cube_id):
        return None
    monkeypatch.setattr(inv, "fetch_cube", unreadable)
    await _borrower(ANN)
    await _queue("q1", [ANN])

    assert (await resv.reserve("q1")).status == "unknown"
    assert (await _row("q1")).library_reserved_at is None


async def test_asking_again_takes_back_your_own_dont_need_it(lounge):
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN, BEN])
    await resv.reserve("q1")
    await resv.release_player("q1", ANN)

    await resv.reclaim_player("q1", ANN)

    assert ANN in await resv.library_users(await _row("q1"))
