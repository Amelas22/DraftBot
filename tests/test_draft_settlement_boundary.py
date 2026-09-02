"""Money moves in one place, and generating a summary is not it.

generate_draft_summary_embed had five production callers -- every match report,
two victory re-renders, room creation, and sticky-message repair in a subsystem
that knows nothing about drafts ending. Settlement fired from all of them. It
was idempotent, so it was safe rather than correct: the trigger for moving real
money was "someone re-rendered an embed".

These tests pin the boundary. The embed builder reports a ledger; the victory
path -- the one function that decides a draft is over -- moves it.
"""
import ast
import inspect
import textwrap

import pytest
import pytest_asyncio

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service


def _money_movers(fn):
    """Calls in fn that move money, by name."""
    moves = ("settle_pool", "settle_draw", "settle_decided_draft", "settle_draft_pool",
             "create_debt_entries_from_stakes", "settle_new_debts",
             "release_draft_pool", "refund_entry", "set_entry")
    src = textwrap.dedent(inspect.getsource(fn))
    return sorted({
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Name) and n.func.id in moves)
             or (isinstance(n.func, ast.Attribute) and n.func.attr in moves))})


def test_generating_a_summary_moves_no_money():
    """The embed builder is a reporter. Anything in this list is a second
    concern that fires on sticky-message repair."""
    import utils

    assert _money_movers(utils.generate_draft_summary_embed) == []


def test_the_victory_path_is_what_settles_a_draft():
    """One trigger, at the transition that decides the draft is over."""
    import utils

    assert "settle_decided_draft" in _money_movers(
        utils.check_and_post_victory_or_draw)


def test_settlement_runs_before_the_victory_transaction_opens():
    """wallet_service takes its own connection and SQLite is single-writer, so
    settling inside the transaction deadlocks the payment against its caller."""
    import utils

    src = textwrap.dedent(inspect.getsource(utils.check_and_post_victory_or_draw))
    fn = ast.parse(src).body[0]
    transactions = [n for n in ast.walk(fn)
                    if isinstance(n, (ast.With, ast.AsyncWith))
                    and "begin" in ast.dump(n.items[0].context_expr)]
    settles = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "settle_decided_draft"]
    assert settles, "the victory path no longer settles"
    inside = [t for t in transactions
              if any(s in ast.walk(t) for s in settles)]
    assert inside == [], (
        "settlement is inside the victory transaction and will deadlock "
        "against the write lock its own caller holds")


@pytest_asyncio.fixture(autouse=True)
async def _seeded(test_db):
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["p2"]))


@pytest.mark.asyncio
async def test_a_draft_with_no_pool_and_no_pairings_settles_to_nothing(test_db):
    """The two regimes discriminate themselves by the data they read: pool
    settlement reads the holder, debt creation reads StakePairing. A draft with
    neither needs no discriminator to leave alone."""
    from utils import settle_decided_draft

    await settle_decided_draft("s1")

    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_the_pool_pays_from_the_victory_path(test_db):
    """End to end through the function the victory path calls."""
    from sqlalchemy import update

    from session import AsyncSessionLocal, DraftSession
    from utils import settle_decided_draft

    for player in ("p1", "p2"):
        await wallet_service.adjust("g", player, 500, f"seed-{player}", "t")
        await pool.set_entry("g", "s1", player, 50)
    await pool.match_pool("g", "s1", ["p1"], ["p2"])
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "s1")
                             .values(session_stage="teams", match_counter=2))
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "s1")
                             .values(sign_ups={"p1": "Ada", "p2": "Brin"}))
    from models.match import MatchResult
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(MatchResult(session_id="s1", match_number=1,
                               player1_id="p1", player1_wins=2,
                               player2_id="p2", player2_wins=0,
                               winner_id="p1", pairing_message_id="0"))

    await settle_decided_draft("s1")

    balances = await wallet_service.balances_for("g", ["p1", "p2"])
    assert balances["p1"] == 550, "the winner was not paid double their entry"
    assert balances["p2"] == 450
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_draft_already_in_play_still_settles_after_the_cutover(test_db):
    """Deploying mid-draft must not strand the drafts being played.

    A draft in the play stage got its StakePairing rows at rooms creation --
    the same function that sets session_stage to 'pairings' -- so it carries
    them across the deploy even though the writer is gone. It holds no pool,
    because its players signed up before entries were charged. Settlement finds
    the empty holder, falls to the debt path, reads those pairings, and books
    what the losers owe exactly as it did before.

    This is the cohort that decides whether the cutover can happen while people
    are playing. The one that cannot is a draft that has NOT reached rooms yet:
    no pairings written, and no pool either.
    """
    from sqlalchemy import select, update

    from models.match import MatchResult
    from models.stake_pairing import StakePairing
    from models.debt_ledger import DebtLedger
    from session import AsyncSessionLocal, DraftSession
    from utils import settle_decided_draft

    A = ["la1", "la2"]
    B = ["lb1", "lb2"]
    await seed_session("legacy1", guild="g", stype="staked",
                       stage="pairings", teams=(A, B),
                       sign_ups={p: p for p in A + B})
    async with AsyncSessionLocal() as db:
        async with db.begin():
            # Written by the old matcher at rooms creation, before the deploy.
            for a, b in zip(A, B):
                db.add(StakePairing(session_id="legacy1", player_a_id=a,
                                    player_b_id=b, amount=25))
            for i, (a, b) in enumerate(zip(A, B)):
                db.add(MatchResult(session_id="legacy1", match_number=i + 1,
                                   player1_id=a, player1_wins=2,
                                   player2_id=b, player2_wins=0,
                                   winner_id=a, pairing_message_id="0"))
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "legacy1")
                             .values(match_counter=3))

    assert await pool.pool_balance("g", "legacy1") == 0, "this draft has no pool"

    await settle_decided_draft("legacy1")

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DebtLedger.player_id, DebtLedger.counterparty_id,
                   DebtLedger.amount)
            .where(DebtLedger.source_type == "draft",
                   DebtLedger.source_id == "legacy1"))).all()

    assert rows, (
        "a draft that was already being played settled to nothing after the "
        "cutover -- its players' stakes vanished mid-draft")
    owed = {(p, c): a for p, c, a in rows}
    for loser, winner in zip(B, A):
        assert owed.get((loser, winner)) == -25, (
            f"{loser} does not owe {winner} the 25 they were paired for: {owed}")
