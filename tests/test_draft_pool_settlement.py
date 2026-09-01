"""The winning team splits the pool in proportion to what each has at risk."""
import pytest
import pytest_asyncio
from sqlalchemy import update

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service
from session import AsyncSessionLocal, DraftSession

A = ["a1", "a2", "a3", "a4"]
B = ["b1", "b2", "b3", "b4"]


async def _matched():
    # Round tens, because that is all a queue can produce: 40/30/20/10 against
    # 30/20/10/10. Team A cannot fit all four bets into 70, so they share it in
    # proportion and land on 30/20/10/10 -- the same 70 team B already holds.
    for player, amount in {"a1": 40, "a2": 30, "a3": 20, "a4": 10,
                           "b1": 30, "b2": 20, "b3": 10, "b4": 10}.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)
    await pool.match_pool("g", "s1", A, B)



@pytest_asyncio.fixture(autouse=True)
async def _an_open_queue(test_db):
    """Every charge in production happens against a draft that exists.

    set_entry now refuses to charge into a session with no row -- a component
    left open after its draft was deleted used to read as an open queue and
    strand the money in a holder nothing would settle. These tests charged
    against ids that were never seeded, so they were exercising exactly that
    hole; the row is what production would have.
    """
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["b1"]))


@pytest.mark.asyncio
async def test_every_matched_tix_pays_at_the_same_rate(test_db):
    """Because both sides are matched to the same total, a winner receives
    exactly twice what they had at risk -- and no teammate's entry can change
    that rate, which is the whole reason for matching."""
    await _matched()

    result = await pool.settle_pool("g", "s1", A)

    assert result["paid"] == {"a1": 60, "a2": 40, "a3": 20, "a4": 20}
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_the_pool_empties_exactly_whichever_side_wins(test_db):
    await _matched()

    result = await pool.settle_pool("g", "s1", B)

    assert sum(result["paid"].values()) == 140
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_settling_twice_pays_once(test_db):
    """check_and_post_victory_or_draw can be re-entered; the source is the guard."""
    await _matched()

    await pool.settle_pool("g", "s1", A)
    await pool.settle_pool("g", "s1", A)

    # entered 40, had 10 handed back as unmatched, then took 60 for the win
    assert await wallet_service.get_balance("g", "a1") == 1000 - 40 + 10 + 60
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_the_payout_is_exactly_double_and_never_divides(test_db):
    """There is no rounding here, and there cannot be.

    Matching levels both sides at M, so the pool is 2M; a winner holding c takes
    2M * c / M = exactly 2c. Deliberately awkward numbers -- 11, 7 and 5 against
    23 -- because if a remainder were possible these would find it.
    """
    for player, amount in {"a1": 110, "a2": 70, "a3": 50, "b1": 230}.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)
    await pool.match_pool("g", "s1", A, B)
    held = await pool.contributions("g", "s1")

    result = await pool.settle_pool("g", "s1", A)

    assert result["paid"] == {p: held[p] * 2 for p in A if held.get(p)}, (
        f"a winner was paid something other than double their matched stake: "
        f"{result['paid']} against held {held}")
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_closed_book_with_unlevel_sides_refuses_everything(test_db):
    """The invariant catches it before any money moves.

    A draft whose book has closed with the sides unequal cannot have a correct
    payout: a winner takes double their MATCHED stake, and nothing here was
    matched. check_pool raises at the top of settle_pool, so the refusal costs
    nothing and the money stays where it is -- recoverable, where paying it out
    wrongly would not be.

    Unreachable by construction: matching levels the sides, and set_entry
    refuses money once the book has closed. This pins that it stays a refusal.
    """
    from conftest import seed_session

    await seed_session("unlevel", guild="g", stype="staked", stage=None,
                       teams=(["a1"], ["b1"]), sign_ups={"a1": "A", "b1": "B"})
    for player, amount in {"a1": 60, "b1": 20}.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "unlevel", player, amount)

    # Close the book WITHOUT matching -- the state the invariant exists to catch.
    async with AsyncSessionLocal() as db:
        await db.execute(update(DraftSession)
                         .where(DraftSession.session_id == "unlevel")
                         .values(session_stage="teams"))
        await db.commit()

    with pytest.raises(pool.PoolInvariantViolated) as raised:
        await pool.settle_pool("g", "unlevel", ["a1"])

    assert "60" in str(raised.value) and "20" in str(raised.value), (
        f"the violation did not name the two sides: {raised.value}")
    assert await pool.pool_balance("g", "unlevel") == 80, (
        "a refusal must leave the money where it is, not partially pay it out")


@pytest.mark.asyncio
async def test_settling_a_pool_nobody_funded_is_harmless(test_db):
    """Most drafts are not staked, and the victory path calls this regardless."""
    assert await pool.settle_pool("g", "never-staked", A) == {"paid": {}}


@pytest.mark.asyncio
async def test_the_pool_replaces_the_debt_path_rather_than_joining_it(test_db):
    """With the pool on, booking stake debts as well would bill the losers a
    second time for a draft the winners have already been paid for."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from conftest import seed_session
    from utils import settle_decided_draft

    NA = [f"70000000000000000{i}" for i in range(4)]
    NB = [f"80000000000000000{i}" for i in range(4)]
    sid = "s1"
    # Open first, so the entries can be paid the way production pays them.
    await seed_session(sid, guild="1", stype="staked", stage=None,
                       teams=(NA, NB),
                       matches=[(NA[i], NB[i], NA[i], None) for i in range(3)],
                       sign_ups={p: p for p in NA + NB})
    for player in NA + NB:
        await wallet_service.adjust("1", player, 500, f"seed-{player}", "t")
        await pool.set_entry("1", sid, player, 20)
    # seed_session leaves match_counter at its default of 1, i.e. no matches.
    # A draft with none is undecided, so nothing would settle at all.
    async with AsyncSessionLocal() as _db:
        await _db.execute(update(DraftSession)
                          .where(DraftSession.session_id == sid)
                          .values(match_counter=4, session_stage="completed"))
        await _db.commit()
    guild = MagicMock()
    guild.get_member.return_value = None   # display names fall back to strings
    guild.roles = []
    bot = MagicMock()
    bot.get_guild.return_value = guild

    with patch("utils.settle_pool", new=AsyncMock()) as settled, \
         patch("utils.create_debt_entries_from_stakes", new=AsyncMock()) as booked:
        await settle_decided_draft("s1")

    settled.assert_awaited_once()
    booked.assert_not_awaited()
    # Which side was paid, not merely that something was. Without this, paying
    # the LOSING team passes.
    paid_team = settled.await_args.args[2]
    assert sorted(paid_team) == sorted(NA), (
        f"settlement paid {paid_team}, but team A won every match")


@pytest.mark.asyncio
async def test_a_pre_conversion_draft_still_settles_by_its_pairings(test_db):
    from unittest.mock import AsyncMock, MagicMock, patch

    from conftest import seed_session
    from utils import settle_decided_draft

    NA = [f"70000000000000000{i}" for i in range(4)]
    NB = [f"80000000000000000{i}" for i in range(4)]
    sid = "s2"
    await seed_session(sid, guild="1", stype="staked", stage="completed",
                       teams=(NA, NB),
                       matches=[(NA[i], NB[i], NA[i], None) for i in range(3)],
                       sign_ups={p: p for p in NA + NB})
    # seed_session leaves match_counter at its default of 1, i.e. no matches.
    # A draft with none is undecided, so nothing would settle at all.
    async with AsyncSessionLocal() as _db:
        await _db.execute(update(DraftSession)
                          .where(DraftSession.session_id == sid)
                          .values(match_counter=4))
        await _db.commit()
    # Stake pairings, because the legacy debt block sits behind `if
    # outcome_lines:` -- without them the flag-off path renders nothing and the
    # test proves only that the pool stayed out, which is half the claim.
    from models.stake_pairing import StakePairing
    from database.db_session import db_session as _db
    async with _db() as _s:
        for i in range(3):
            _s.add(StakePairing(session_id="s2", player_a_id=NA[i],
                                player_b_id=NB[i], amount=10))
    guild = MagicMock()
    guild.get_member.return_value = None   # display names fall back to strings
    guild.roles = []
    bot = MagicMock()
    bot.get_guild.return_value = guild

    # This draft carries StakePairing rows, so it is pre-conversion and must
    # finish on the debt path it was started under.
    with patch("utils.settle_pool", new=AsyncMock()) as settled, \
         patch("utils.create_debt_entries_from_stakes", new=AsyncMock()) as booked, \
         patch("utils.settle_new_debts", new=AsyncMock()):
        await settle_decided_draft("s2")

    settled.assert_not_awaited()
    # And the legacy path must actually RUN. Asserting only that the pool stayed
    # out would pass if the debt path had been deleted outright.
    booked.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_draw_returns_every_entry_rather_than_stranding_it(test_db):
    """A draw pays nobody -- but the money still has to go somewhere.

    Settlement is victory-only by design, so a drawn draft never calls
    settle_pool. If nothing else empties the holder, every player's entry is
    stuck in prize:draft:<id> with the draft already marked completed and no
    later path that will ever attribute it. Paying nobody must mean refunding
    everybody, not keeping it.
    """
    for player, amount in {"a1": 40, "b1": 40}.items():
        await wallet_service.adjust("g", player, 100, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)
    await pool.match_pool("g", "s1", A, B)
    assert await pool.pool_balance("g", "s1") == 80

    await pool.settle_draw("g", "s1")

    assert await pool.pool_balance("g", "s1") == 0, "a drawn draft stranded the pool"
    assert await wallet_service.get_balance("g", "a1") == 100
    assert await wallet_service.get_balance("g", "b1") == 100


@pytest.mark.asyncio
async def test_a_payout_that_fails_partway_leaves_the_pool_settleable(test_db):
    """Winners are paid together or not at all.

    Paying them one transaction at a time looks harmless because each transfer
    is idempotent, but a failure between two of them commits the first and
    leaves the holder half empty -- and the two sides no longer level. The next
    attempt runs check_pool first, sees the imbalance it caused, and refuses.
    The draft becomes unsettleable by an error in the middle of paying it out.
    """
    from unittest.mock import AsyncMock, patch

    from services import wallet_service

    await _matched()
    before = await pool.pool_balance("g", "s1")
    assert before > 0

    real = wallet_service.transfer_in
    calls = {"n": 0}

    async def _fail_on_the_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection dropped mid-payout")
        return await real(*args, **kwargs)

    with patch.object(wallet_service, "transfer_in", new=_fail_on_the_second):
        with pytest.raises(RuntimeError):
            await pool.settle_pool("g", "s1", A)

    assert await pool.pool_balance("g", "s1") == before, (
        "a failed payout left tix out of the holder; the sides are now unequal "
        "and check_pool will refuse every retry")
    await pool.check_pool("g", "s1")          # still a valid pool

    result = await pool.settle_pool("g", "s1", A)
    assert sum(result["paid"].values()) == before
    assert await pool.pool_balance("g", "s1") == 0
