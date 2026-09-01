"""The pool is the MATCHED portion; unmatched entry goes back before the draft.

Why cap at all: unmatched money has no prize to be paid out of. And without the
cap a winner's share is (own entry / own team total) x pool, so the payout
multiple is 1 + (other team total / own team total) -- every tix a teammate adds
dilutes everyone else on that team. Matching makes every matched tix pay at the
same rate, so a teammate's entry can change how much of yours is matched but
never the rate it pays.
"""
import ast
from pathlib import Path

import pytest
import pytest_asyncio

from services import draft_pool_service as pool
from sqlalchemy import update

from conftest import seed_session
from session import AsyncSessionLocal, DraftSession
from services import wallet_service

A = ["a1", "a2", "a3", "a4"]
B = ["b1", "b2", "b3", "b4"]


async def _fund(entries):
    for player, amount in entries.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)



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


def _team_creator_ast():
    """The parsed body of create_and_display_teams.

    Two structural facts below used to be checked by searching the source text:
    a 400-character window between two substrings, and a count of leading
    spaces. Both named the right hazard, and both would break on a reformat or
    a new comment while saying nothing useful about why. The tree answers the
    same questions exactly.
    """
    import ast
    import inspect

    from services.team_creator import create_and_display_teams

    tree = ast.parse(inspect.getsource(create_and_display_teams))
    return ast.walk, tree.body[0]


def _calls_named(node, name):
    import ast

    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


@pytest.mark.asyncio
async def test_small_bets_are_filled_before_large_ones(test_db):
    """A 20-tix bet is a whole bet, not a fraction of somebody else's.

    Scaling every entry by the same ratio is the intuitive split and the wrong
    one: it shaves the player who bet the draft MINIMUM down below that
    minimum, to pay for headroom on a bet three times the size. The queue embed
    has always promised the opposite -- fill the small bets, then share what is
    left -- and this is that promise.

    These are the numbers from a real draft: 20 + 170 + 200 against 220. Pro
    rata gave 12 / 96 / 112, putting the 20-tix player under the 20-tix floor.
    """
    await _fund({"a1": 20, "a2": 170, "a3": 200,
                 "b1": 60, "b2": 50, "b3": 110})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["matched"] == 220
    held = await pool.contributions("g", "s1")
    assert held["a1"] == 20, "the smallest bet was cut to fund a larger one"
    # 200 of room left between the two big bets, split in proportion to them:
    # 170/370 and 200/370, rounded to tens.
    assert held["a2"] == 90 and held["a3"] == 110, (
        f"the remainder was not shared in proportion to the bets: {held}")
    assert sum(held[p] for p in ("b1", "b2", "b3")) == 220


@pytest.mark.asyncio
async def test_every_matched_stake_is_a_round_number(test_db):
    """Tix are wagered in tens; a matched stake of 96 or 112 is not a bet
    anyone placed. Levelling rounds down to the ten, and the remainder goes
    back to the player rather than being carried as dust."""
    await _fund({"a1": 20, "a2": 170, "a3": 200,
                 "b1": 60, "b2": 50, "b3": 110})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    ragged = {p: n for p, n in held.items() if n % 10}
    assert ragged == {}, f"matched stakes that are not multiples of ten: {ragged}"


@pytest.mark.asyncio
async def test_a_bet_is_never_raised_by_levelling(test_db):
    """Filling small bets first must never hand someone MORE at risk than they
    agreed to -- the cap is always their own entry."""
    await _fund({"a1": 20, "a2": 170, "a3": 200,
                 "b1": 60, "b2": 50, "b3": 110})
    before = await pool.contributions("g", "s1")

    await pool.match_pool("g", "s1", A, B)

    after = await pool.contributions("g", "s1")
    for player, was in before.items():
        assert after.get(player, 0) <= was, (
            f"{player} was levelled UP from {was} to {after.get(player)}")


@pytest.mark.asyncio
async def test_when_the_budget_cannot_fill_even_the_small_bets(test_db):
    """Three players against a side holding 30: nobody's bet fits whole, so the
    fair thing is an equal share, still in tens."""
    await _fund({"a1": 100, "a2": 100, "a3": 100, "b1": 30})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["matched"] == 30
    held = await pool.contributions("g", "s1")
    a_side = sorted(held.get(p, 0) for p in ("a1", "a2", "a3"))
    assert sum(a_side) == 30, f"the side does not total the match: {a_side}"
    assert all(n % 10 == 0 for n in a_side), a_side
    assert a_side == [10, 10, 10], f"the proportional share was not equal: {a_side}"


@pytest.mark.asyncio
async def test_the_refunds_sum_exactly_to_the_excess(test_db):
    """Pro rata with integer tix leaves a remainder. Dropping it would strand
    dust in the holder forever; over-refunding would come out of Team B."""
    await _fund({"a1": 34, "a2": 33, "a3": 33, "b1": 70})

    await pool.match_pool("g", "s1", A, B)

    assert await pool.pool_balance("g", "s1") == 140
    assert sum((await pool.contributions("g", "s1")).values()) == 140


@pytest.mark.asyncio
async def test_equal_sides_are_left_alone(test_db):
    await _fund({"a1": 50, "b1": 50})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["refunded"] == {}
    assert await pool.pool_balance("g", "s1") == 100


@pytest.mark.asyncio
async def test_matching_twice_refunds_once(test_db):
    """team_creator can be re-entered; a retry must not drain the pool."""
    await _fund({"a1": 100, "b1": 70})

    await pool.match_pool("g", "s1", A, B)
    await pool.match_pool("g", "s1", A, B)

    assert await pool.pool_balance("g", "s1") == 140


@pytest.mark.asyncio
async def test_a_side_that_paid_nothing_refunds_the_other_entirely(test_db):
    """Nobody on B entered, so there is no prize for A's money to play for and
    it must all go back rather than sit in the holder until teardown."""
    await _fund({"a1": 40})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["matched"] == 0
    assert await pool.pool_balance("g", "s1") == 0
    assert await wallet_service.get_balance("g", "a1") == 1000


def test_matching_runs_only_once_the_teams_actually_exist():
    """The stage write is NOT the right anchor.

    `session_stage = 'teams'` is set before split_into_teams runs, so for a
    random or staked draft -- the dominant session type -- team_a and team_b are
    still NULL at that moment. Matching anchored there sees two empty sides,
    computes min(0, 0) = 0, and silently does nothing: every entry stays
    unmatched, the pool is never levelled, and settlement later divides by
    numbers matching should have corrected.

    It has to run after the teams are known.
    """
    import inspect

    from services.team_creator import create_and_display_teams

    src = inspect.getsource(create_and_display_teams)
    split_at = src.find("split_into_teams(")
    match_at = src.find("match_pool(")
    assert match_at != -1, "team creation never closes the book"
    assert split_at != -1, "split_into_teams moved; re-check this assertion"
    assert split_at < match_at, (
        "match_pool runs BEFORE split_into_teams, so team_a/team_b are still "
        "NULL for random and staked drafts and matching silently no-ops")


def test_the_capture_gate_matches_the_settlement_gate():
    """Matching and settlement must agree on which drafts have a pool.

    Level a pool wider than settlement pays and the excess is levelled into a
    holder nothing will ever empty; narrower, and a funded draft reaches its
    victory with unlevel sides and no derivable payout. Entries can only arrive
    through the staked signup UI and settlement is gated on session_type ==
    "staked", so the capture is gated on exactly that and nothing else.
    """
    import ast

    _, fn = _team_creator_ast()
    captures = [n for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "pool_sides"
                        for t in n.targets)
                and not isinstance(n.value, ast.Constant)]
    assert captures, "the sides are never captured"

    gates = [b for b in ast.walk(fn) if isinstance(b, ast.If)
             and any(c in ast.walk(b) for c in captures)]
    assert len(gates) == 1, f"the capture sits under {len(gates)} branches, not one"
    condition = ast.dump(gates[0].test)
    assert "'staked'" in condition or '"staked"' in condition, (
        f"the capture is gated on {condition}, which is not the gate settlement "
        "uses -- one of them will see a draft the other does not")
    for other in ("random", "premade", "winston", "test", "swiss"):
        assert f"'{other}'" not in condition, (
            f"{other} drafts would have a pool levelled that settlement never "
            "pays out, stranding it in the holder")


def test_matching_runs_outside_the_team_creation_transaction():
    """wallet_service.pay opens its own connection, and SQLite is single-writer.

    Called while create_and_display_teams still holds the write lock, the inner
    write waits on a lock only the outer transaction can release -- it times
    out, the retry loop exhausts, and the whole transaction rolls back. The
    symptom is a draft with teams written (split_into_teams commits separately)
    and a session_stage that never advanced, which is exactly what a live bot
    produced before this was moved.
    """
    import ast

    _, fn = _team_creator_ast()
    transactions = [n for n in ast.walk(fn)
                    if isinstance(n, (ast.With, ast.AsyncWith))
                    and "begin" in ast.dump(n.items[0].context_expr)]
    assert transactions, "the team-creation transaction moved; re-check this guard"
    assert _calls_named(fn, "match_pool"), "team creation never closes the book"
    inside = [t for t in transactions if _calls_named(t, "match_pool")]
    assert inside == [], (
        "match_pool is lexically inside the team-creation transaction, so its "
        "wallet writes will wait on a lock only the enclosing transaction can "
        "release")


def test_the_staked_path_reaches_the_book_closing():
    """Staked drafts are the ONLY ones with a pool, and they used to `return
    True` from inside the team-creation transaction -- before the line that
    closes the book. Matching was therefore unreachable for exactly the drafts
    that needed it, while every service-level test stayed green because they
    called match_pool directly.

    The staked branch must fall out of the transaction, not return from inside
    it: the money cannot move while that transaction holds the write lock.
    """
    import inspect

    from services.team_creator import create_and_display_teams

    src = inspect.getsource(create_and_display_teams)
    staked_at = src.find('if persistent_view.session_type == "staked":')
    match_at = src.find("await match_pool(")
    assert staked_at != -1 and match_at != -1
    between = src[staked_at:match_at]
    assert "return True" not in between, (
        "the staked branch returns before the book is closed, so matching never "
        "runs for the only drafts that have a pool")
    assert "staked_done = True" in between, (
        "the staked branch no longer records that it handled the draft")


def test_matching_and_settlement_agree_on_which_drafts_have_a_pool():
    """If matching is wider than settlement, a draft can have its pool levelled
    by one and never paid by the other -- money stranded by construction.

    Settlement lives inside `if draft_session.session_type == "staked"` in
    generate_draft_summary_embed. Matching must gate on the same thing.
    """
    import inspect

    import utils
    from services.team_creator import create_and_display_teams

    match_src = inspect.getsource(create_and_display_teams)

    assert 'session.session_type == "staked"' in match_src, (
        "matching does not gate on staked, so it can level a pool that "
        "settlement will never pay out")
    settle_src = inspect.getsource(utils.settle_decided_draft)
    assert 'draft_session.session_type != "staked"' in settle_src, (
        "the settlement gate moved; re-check that matching still matches it")


def test_no_production_code_still_runs_the_tiered_matcher():
    """One matcher, or the numbers disagree.

    draft_organization/stake_calculator.py is 1,638 lines solving a problem the
    pool deletes: who is paired with whom, at what amount. Running it alongside
    match_pool writes StakePairing rows by one rule while the money moves by
    another -- and worse, uses_legacy_stakes reads those rows to decide which
    settlement path a draft takes. A single surviving writer sends every new
    draft down the legacy path with its entry fees stranded in the holder.

    The predecessor of this test grepped one call site in team_creator, which is
    why a second writer in views.create_rooms_pairings survived it. This one
    asks the question that actually matters: does *any* shipping module still
    reach the matcher?
    """
    roots = ("cogs", "database", "draft_organization", "helpers", "models",
             "services", "views.py", "utils.py", "livedrafts.py", "bot.py")
    callers = []
    for root in roots:
        path = Path(root)
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for f in files:
            if f.name == "stake_service.py":
                continue  # the definition itself, kept for historical backfills
            tree = ast.parse(f.read_text())
            # A name in code, not a mention in a docstring or comment.
            named = any(
                (isinstance(n, ast.Name) and n.id == "calculate_and_store_stakes")
                or (isinstance(n, ast.Attribute) and n.attr == "calculate_and_store_stakes")
                or (isinstance(n, ast.alias) and n.name == "calculate_and_store_stakes")
                for n in ast.walk(tree))
            if named:
                callers.append(str(f))

    assert callers == [], (
        f"{callers} still writes StakePairing rows. Every draft it touches "
        "reads as legacy at settlement, so the pool it was funded from is "
        "never paid out.")


@pytest.mark.asyncio
async def test_every_display_surface_reads_the_pool_for_a_pool_draft(test_db):
    """Four surfaces render a staked draft's money -- the teams embed, the
    live-draft panel, the summary embed, and the stake-breakdown button -- and
    all four go through get_formatted_stake_pairs. A pool draft has no
    StakePairing rows, so any surface still reading pairings shows an empty
    field for a pot that is already funded and about to be played for.

    This asks the formatter directly rather than grepping a caller: the branch
    used to be copied into call sites, and the copies that were missed were
    invisible to a test that only read the one that wasn't.
    """
    from utils import get_formatted_stake_pairs

    # The real sequence: entries are paid while the queue is open, then the
    # book closes when teams form and the embed renders.
    await seed_session(session_id="disp", guild="g", stage=None,
                       teams=(["p1", "p2"], ["p3", "p4"]))
    for player, amount in {"p1": 30, "p2": 20, "p3": 30, "p4": 20}.items():
        await wallet_service.adjust("g", player, 500, f"seed-{player}", "test")
        await pool.set_entry("g", "disp", player, amount)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "disp")
                             .values(session_stage="teams"))

    names = {"p1": "Ada", "p2": "Brin", "p3": "Cyd", "p4": "Dov"}
    lines, total = await get_formatted_stake_pairs("disp", names)

    assert total == 100, (
        "the surface reports 0 tix at risk for a draft holding 100 -- it is "
        "still reading StakePairing rows that a pool draft never has")
    assert any("Ada" in line and "30" in line for line in lines), lines


def test_a_pool_draft_is_not_offered_a_settle_debts_button():
    """No debt exists under the pool; the button invites a player to pay
    something they do not owe."""
    import inspect

    import utils

    # The victory path owns that button now, and asks the only question that
    # matters: is there a debt to settle? Under the pool there never is.
    src = inspect.getsource(utils.check_and_post_victory_or_draw)
    settle_at = src.find("SettleDebtsView(")
    assert settle_at != -1, "the button moved; re-check this guard"
    guard = src[:settle_at]
    assert "get_draft_debtors" in guard, (
        "a Settle Debts button is offered without asking whether this draft "
        "made anyone a debtor, so a pool draft gets one")


@pytest.mark.asyncio
async def test_a_bet_at_or_below_the_tier_is_filled_before_the_big_ones(test_db):
    """Everything up to 50 is a small bet and is paid in full first; only what
    is left over is shared out among the bets above that."""
    await _fund({"a1": 50, "a2": 20, "a3": 300,
                 "b1": 100, "b2": 100, "b3": 40})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert held["a1"] == 50 and held["a2"] == 20, (
        f"a bet inside the small tier was cut: {held}")
    assert held["a3"] == 170, f"the large bet did not take the remainder: {held}"


@pytest.mark.asyncio
async def test_the_bigger_of_two_large_bets_keeps_more_at_risk(test_db):
    """Proportional, not equal: backing twice as much means carrying twice the
    shortfall, and keeping more on the table for it."""
    await _fund({"a1": 100, "a2": 300, "b1": 200})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert held["a2"] > held["a1"], (
        f"the larger bet was levelled to the smaller one: {held}")
    assert held["a1"] + held["a2"] == 200
