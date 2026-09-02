"""Money into a draft's pool, and the refusal when a player cannot cover it.

The invariant this serves: no new tix obligation can be created. An entry that
cannot be funded is not accepted, so the refusal path matters as much as the
transfer -- a player who is short is told the shortfall rather than taking on a
debt they will owe after the draft.
"""
import pytest
import pytest_asyncio
from sqlalchemy import update

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service
from session import AsyncSessionLocal, DraftSession


def test_the_holder_is_a_system_account():
    """So it never appears in a leaderboard, a debt list, or a wallet roster.
    is_system_account keys on "not a Discord snowflake", and our id is not one."""
    assert wallet_service.is_system_account(pool.pool_wallet_id("abc-123"))



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
async def test_an_entry_moves_tix_from_the_player_into_the_pool(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")

    result = await pool.set_entry("g", "s1", "p1", 40)

    assert result["ok"] is True
    assert await wallet_service.get_balance("g", "p1") == 60
    assert await wallet_service.get_balance("g", pool.pool_wallet_id("s1")) == 40


@pytest.mark.asyncio
async def test_an_unfunded_player_is_refused_and_told_the_shortfall(test_db):
    """Refusing is the point: the obligation is funded from the moment it is
    taken on, or it is not taken on. The deficit is what the caller shows."""
    await wallet_service.adjust("g", "p1", 10, "seed", "test")

    result = await pool.set_entry("g", "s1", "p1", 40)

    assert result == {"ok": False, "deficit": 30}
    assert await wallet_service.get_balance("g", "p1") == 10
    assert await wallet_service.get_balance("g", pool.pool_wallet_id("s1")) == 0


@pytest.mark.asyncio
async def test_charging_the_same_entry_twice_moves_money_once(test_db):
    """A double-click, a retry, a reconnect: the source string is the guard."""
    await wallet_service.adjust("g", "p1", 100, "seed", "test")

    await pool.set_entry("g", "s1", "p1", 40)
    await pool.set_entry("g", "s1", "p1", 40)

    assert await wallet_service.get_balance("g", "p1") == 60
    assert await wallet_service.get_balance("g", pool.pool_wallet_id("s1")) == 40


@pytest.mark.asyncio
async def test_a_retry_succeeds_even_if_the_player_has_since_spent_down(test_db):
    """The order of the two checks matters. A player enters for 40, then spends
    the rest elsewhere; a retry must see that the entry is ALREADY PAID rather
    than reading the now-thin wallet and reporting a shortfall for money that is
    sitting in the pool.
    """
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)
    await wallet_service.pay("g", "p1", "someone", 55, source="spent-elsewhere")

    result = await pool.set_entry("g", "s1", "p1", 40)

    assert result["ok"] is True, (
        f"a paid entry was reported unpayable on retry: {result}")
    assert await wallet_service.get_balance("g", pool.pool_wallet_id("s1")) == 40


# --- refunds, and reading what is actually at risk ---------------------------

@pytest.mark.asyncio
async def test_leaving_the_queue_returns_the_entry(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)

    assert await pool.refund_entry("g", "s1", "p1", 40, "left") is True
    assert await wallet_service.get_balance("g", "p1") == 100
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_refund_is_idempotent(test_db):
    """Half of all staked drafts never fire, so refunds run more often than
    payouts do. A sweeper retrying one must not drain the pool."""
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)

    await pool.refund_entry("g", "s1", "p1", 40, "left")
    await pool.refund_entry("g", "s1", "p1", 40, "left")

    assert await wallet_service.get_balance("g", "p1") == 100


@pytest.mark.asyncio
async def test_contributions_net_entries_against_refunds(test_db):
    """What each player has AT RISK -- the number settlement divides by. Read
    from the ledger rather than from StakeInfo, so it reflects money that
    actually moved."""
    for p in ("p1", "p2"):
        await wallet_service.adjust("g", p, 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)
    await pool.set_entry("g", "s1", "p2", 30)
    await pool.refund_entry("g", "s1", "p1", 10, "unmatched")

    assert await pool.contributions("g", "s1") == {"p1": 30, "p2": 30}


@pytest.mark.asyncio
async def test_a_fully_refunded_player_is_not_a_contributor(test_db):
    """Otherwise settlement would divide by them and pay out a zero share."""
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)
    await pool.refund_entry("g", "s1", "p1", 40, "left")

    assert await pool.contributions("g", "s1") == {}


@pytest.mark.asyncio
async def test_a_refund_the_pool_cannot_cover_is_refused(test_db):
    """Rather than driving the holder negative, which would corrupt
    reconciliation and invent tix that were never deposited."""
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)

    assert await pool.refund_entry("g", "s1", "p1", 50, "left") is False
    assert await pool.pool_balance("g", "s1") == 40


# --- changing a stake after joining ------------------------------------------
# The queue lets a player revise their stake. The pool must follow the change,
# or the ledger and the declared stake drift apart.

@pytest.mark.asyncio
async def test_raising_a_stake_charges_only_the_difference(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 20)

    result = await pool.set_entry("g", "s1", "p1", 50)

    assert result["ok"] is True
    assert await pool.contributions("g", "s1") == {"p1": 50}
    assert await wallet_service.get_balance("g", "p1") == 50


@pytest.mark.asyncio
async def test_lowering_a_stake_returns_the_difference(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 50)

    result = await pool.set_entry("g", "s1", "p1", 20)

    assert result["ok"] is True
    assert await pool.contributions("g", "s1") == {"p1": 20}
    assert await wallet_service.get_balance("g", "p1") == 80


@pytest.mark.asyncio
async def test_a_raise_the_player_cannot_afford_is_refused_and_leaves_the_old_stake(test_db):
    """Refusing must not strand them between two amounts: the original entry
    stays exactly as it was."""
    await wallet_service.adjust("g", "p1", 30, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 20)

    result = await pool.set_entry("g", "s1", "p1", 50)

    assert result == {"ok": False, "deficit": 20}
    assert await pool.contributions("g", "s1") == {"p1": 20}


@pytest.mark.asyncio
async def test_setting_the_same_amount_moves_nothing(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)

    assert (await pool.set_entry("g", "s1", "p1", 40))["ok"] is True
    assert await wallet_service.get_balance("g", "p1") == 60


@pytest.mark.asyncio
async def test_contributions_survive_a_holder_with_many_transactions(test_db):
    """get_history clamps its limit to 100 rows, silently. A pool that has seen
    more movement than that -- eight entries, their unmatched refunds, later
    payouts -- would drop the oldest rows and understate what a player has at
    risk, which is the number settlement divides by. Read it with an aggregate,
    not a page of recent history.
    """
    await wallet_service.adjust("g", "p1", 10_000, "seed", "test")
    # Each transfer writes ONE row against the holder, so this needs >100 of
    # them: one entry of 150, then 110 refunds of 1 -- 111 holder rows, net 40.
    await pool.set_entry("g", "s1", "p1", 150)
    for n in range(110):
        await pool.refund_entry("g", "s1", "p1", 1, f"trim-{n}")

    assert await pool.pool_balance("g", "s1") == 40, "the ledger itself is wrong"
    assert await pool.contributions("g", "s1") == {"p1": 40}, (
        "contributions disagreed with the holder's own balance -- rows were "
        "dropped by the 100-row history clamp")


@pytest.mark.asyncio
async def test_a_player_who_leaves_and_rejoins_pays_again(test_db):
    """The source key is per (draft, player), so a rejoin looks like a retry.

    Join, leave (refunded in full), then join again: the entry must be taken a
    second time. Treating it as already-settled would seat a player in a staked
    draft holding none of their money -- and settlement would then pay them a
    share of everyone else's.
    """
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)
    await pool.refund_entry("g", "s1", "p1", 40, "left")
    assert await wallet_service.get_balance("g", "p1") == 100

    result = await pool.set_entry("g", "s1", "p1", 40)

    assert result["ok"] is True
    assert await pool.contributions("g", "s1") == {"p1": 40}, (
        "the rejoining player is in the draft with nothing in the pool")
    assert await wallet_service.get_balance("g", "p1") == 60


@pytest.mark.asyncio
async def test_a_refund_cannot_be_paid_out_of_another_player_s_entry(test_db):
    """The holder's total is not the right ceiling -- it is everyone's money.
    A refund larger than THIS player's own contribution would pay them out of
    their opponents' entries and still leave the holder positive, so it would
    pass a balance check and corrupt the pool silently.
    """
    for p in ("p1", "p2"):
        await wallet_service.adjust("g", p, 100, "seed", "test")
        await pool.set_entry("g", "s1", p, 40)

    assert await pool.refund_entry("g", "s1", "p1", 70, "greedy") is False
    assert await pool.contributions("g", "s1") == {"p1": 40, "p2": 40}


@pytest.mark.asyncio
async def test_contributions_never_report_a_negative_holding(test_db):
    """A payout is money leaving the holder TO a player, which nets negative
    against their entry. Counting that as a contribution makes a settled pool
    report players as holding less than nothing -- and match_pool then computes
    a negative matched total. Only what is still at risk counts.
    """
    for p in ("p1", "p2"):
        await wallet_service.adjust("g", p, 200, "seed", "test")
        await pool.set_entry("g", "s1", p, 40)
    await pool.settle_pool("g", "s1", ["p1"])   # p1 is paid 80 out of the holder

    held = await pool.contributions("g", "s1")

    assert all(v > 0 for v in held.values()), (
        f"contributions reported a negative holding: {held}")
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_leaving_twice_across_a_rejoin_refunds_both_times(test_db):
    """The refund source has the same collision the entry source had.

    Join, leave, rejoin, leave again: the second leave builds the same
    `draft-refund:left:<session>:<player>` key as the first, so pay() treats it
    as already settled and moves nothing -- while set_entry reports success and
    the sign-up removal goes ahead. The player is out of the queue with their
    money still in the pool.
    """
    await wallet_service.adjust("g", "p1", 100, "seed", "test")

    await pool.set_entry("g", "s1", "p1", 50, "joined")
    await pool.set_entry("g", "s1", "p1", 0, "left")
    await pool.set_entry("g", "s1", "p1", 50, "joined")
    await pool.set_entry("g", "s1", "p1", 0, "left")

    assert await wallet_service.get_balance("g", "p1") == 100, (
        "the second leave returned nothing -- the refund key collided with the "
        "first leave and the money is stranded in the pool")
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_an_entry_cannot_arrive_after_the_book_has_closed(test_db):
    """The queue is the only time an entry may be taken.

    A stake select or modal opened while queueing can be submitted minutes
    later, after teams are formed. Money arriving then belongs to nobody the
    matching pass considered: the sides are already levelled, so the holder
    stops being twice the winning side and settlement has no correct answer.
    That is the state that must be unreachable, so the entry is refused here
    rather than reconciled later.
    """
    from conftest import seed_session

    await seed_session("s1", guild="g", stype="staked", stage="teams",
                       teams=(["a1"], ["b1"]))
    await wallet_service.adjust("g", "late", 100, "seed", "test")

    result = await pool.set_entry("g", "s1", "late", 40, "joined")

    assert result["ok"] is False, "an entry was accepted after teams were formed"
    assert await pool.contributions("g", "s1") == {}
    assert await wallet_service.get_balance("g", "late") == 100


@pytest.mark.asyncio
async def test_a_refund_is_still_allowed_after_the_book_closes(test_db):
    """Matching itself refunds the unmatched excess, and every teardown path
    refunds after the draft has moved on. Only money going IN is barred."""
    from conftest import seed_session

    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["b1"]))
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40, "joined")

    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "s1")
                        .values(session_stage="teams"))
        await s.commit()

    assert (await pool.set_entry("g", "s1", "p1", 0, "left"))["ok"] is True
    assert await wallet_service.get_balance("g", "p1") == 100


@pytest.mark.asyncio
async def test_a_stale_panel_cannot_lower_a_stake_after_the_book_closes(test_db):
    """Late money may not move in EITHER direction.

    The guard refused late increases, on the reasoning that money arriving
    after matching belongs to nobody the levelling pass considered. A late
    decrease is the same fault in reverse and worse: the refund succeeds, that
    player's side is now lighter than the other, and every later mutation --
    including the payout itself -- hits the levelness invariant and raises. The
    draft becomes unsettleable by a click on a panel left open in a scrollback.
    """
    await seed_session(session_id="late", guild="g", stage=None,
                       teams=(["p1"], ["p2"]))
    for player in ("p1", "p2"):
        await wallet_service.adjust("g", player, 500, f"seed-{player}", "t")
        await pool.set_entry("g", "late", player, 50)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "late")
                             .values(session_stage="teams"))

    result = await pool.set_entry("g", "late", "p1", 20)

    assert result["ok"] is False, "a stale panel lowered a stake after matching"
    assert await pool.held_by("g", "late", "p1") == 50
    await pool.check_pool("g", "late")   # still level, still settleable


@pytest.mark.asyncio
async def test_money_cannot_be_charged_into_a_draft_that_does_not_exist(test_db):
    """A queue with no row is not an open queue.

    _queue_is_open read session_stage and returned `stage is None`, which is
    also what a missing row yields -- so a component left open after its draft
    was deleted read as open and charged the player in. The tix land in a
    holder for a session nothing will ever settle or tear down, and check_pool
    cannot see them either: it also returns early when the row is gone.
    """
    await wallet_service.adjust("g", "p1", 500, "seed", "t")

    result = await pool.set_entry("g", "ghost", "p1", 40)

    assert result["ok"] is False, "charged a player into a draft that is gone"
    assert await pool.pool_balance("g", "ghost") == 0
    assert (await wallet_service.balances_for("g", ["p1"]))["p1"] == 500
