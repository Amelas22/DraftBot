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

from conftest import seed_session, seed_stakes
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
    minimum, to pay for headroom on a bet three times the size.

    These are the numbers from a real draft: 20 + 170 + 200 against 220. Pro
    rata gave 12 / 96 / 112, putting the 20-tix player under the 20-tix floor.

    Levelling fills to a common ceiling instead, so the 20 clears it untouched
    and the two big bets meet at 100 each. Note that the small bet is filled
    whole as a CONSEQUENCE of the ceiling clearing it, not because it belongs
    to a protected tier -- see test_a_small_bet_is_cut_when_the_pot_is_tight.
    """
    await _fund({"a1": 20, "a2": 170, "a3": 200,
                 "b1": 60, "b2": 50, "b3": 110})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["matched"] == 220
    held = await pool.contributions("g", "s1")
    assert held["a1"] == 20, "the smallest bet was cut to fund a larger one"
    # 220 of room; the ceiling settles at 100, which the 20 clears outright.
    assert held["a2"] == 100 and held["a3"] == 100, (
        f"the two large bets were not levelled to a common ceiling: {held}")
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
async def test_a_bet_under_the_ceiling_is_left_whole(test_db):
    """A bet the ceiling clears is paid in full; only bets above it are cut."""
    await _fund({"a1": 50, "a2": 20, "a3": 300,
                 "b1": 100, "b2": 100, "b3": 40})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert held["a1"] == 50 and held["a2"] == 20, (
        f"a bet below the ceiling was cut: {held}")
    assert held["a3"] == 170, f"the large bet did not take the remainder: {held}"


@pytest.mark.asyncio
async def test_two_large_bets_are_levelled_to_a_common_ceiling(test_db):
    """Equal, not proportional: the shortfall is carried by whoever is ABOVE
    the ceiling, and only down as far as the ceiling.

    Proportional is the intuitive rule and it lets one large bet set everyone
    else's divisor. In a real shape -- 60 and 400 against 50 and 50 -- pro rata
    ground the 60-tix player down to 10 to buy headroom for the 400, which is
    the same squeeze the small-bet case exists to prevent, one tier up.

    Here 100 and 300 share 200: the ceiling settles at 100, so the smaller bet
    rides untouched and the larger carries the whole shortfall.
    """
    await _fund({"a1": 100, "a2": 300, "b1": 200})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert held["a1"] == 100 and held["a2"] == 100, (
        f"the shortfall was shared pro rata instead of levelled: {held}")
    assert held["a1"] + held["a2"] == 200


@pytest.mark.asyncio
async def test_nobody_who_bet_more_ends_up_holding_less(test_db):
    """The property the ceiling buys, and the one pro rata could not hold.

    Rounding a proportional share DOWN to a whole ten can drop a larger bet
    below a smaller one that happened to round up. A common ceiling cannot:
    every allocation is min(own bet, ceiling), which is monotone in the bet.
    """
    await _fund({"a1": 100, "a2": 150, "a3": 200, "a4": 250,
                 "b1": 130, "b2": 130, "b3": 130, "b4": 130})

    await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    bets = {"a1": 100, "a2": 150, "a3": 200, "a4": 250}
    by_bet = [held.get(p, 0) for p in sorted(bets, key=bets.__getitem__)]
    assert by_bet == sorted(by_bet), (
        f"holdings are not monotone in bet size: {by_bet} for {bets} in {held}")


@pytest.mark.asyncio
async def test_a_small_bet_is_cut_when_the_pot_is_tight(test_db):
    """Small bets are filled whole because the ceiling usually clears them --
    NOT because they are exempt. When the other side cannot cover even the
    small bets, the ceiling drops below them and they are cut like any other.

    This is the honest edge of the rule and it is worth pinning: the old tier
    language implied a floor under every bet at or below 50, and there is none.
    """
    await _fund({"a1": 50, "a2": 50, "a3": 50, "a4": 50,
                 "b1": 20, "b2": 20, "b3": 20, "b4": 20})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["matched"] == 80
    held = await pool.contributions("g", "s1")
    assert [held[p] for p in A] == [20, 20, 20, 20], (
        f"the side over the ceiling was not levelled onto it: {held}")


def test_every_path_into_matching_holds_the_team_creation_flag():
    """match_pool is safe to REPLAY but not to run twice at once.

    It reads every holding once and commits the refunds one at a time, so two
    overlapping calls would trim the same side from the same snapshot twice and
    leave check_pool raising on an imbalance already booked. Nothing inside the
    function can prevent that -- a lock there would not help, because it is the
    READ that goes stale. The only thing standing between the pool and a double
    trim is that every caller sets state_manager.is_creating_teams first.

    So the invariant is about call sites, and this is what checks it. A new
    entry point that forgets the flag is exactly the change that reopens the
    hole, and it would otherwise look completely innocent in review.
    """
    import ast
    from pathlib import Path

    callers = []
    for module in ("views.py", "ready_check.py", "services/team_creator.py"):
        tree = ast.parse(Path(module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "create_and_display_teams" not in names:
                continue
            callers.append(f"{module}:{node.name}")
            guarded = {n.func.attr for n in ast.walk(node)
                       if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute)}
            assert "is_creating_teams" in guarded and "set_creating_teams" in guarded, (
                f"{module}:{node.name} calls create_and_display_teams without "
                f"taking state_manager.is_creating_teams first, so two of them "
                f"can reach match_pool at once and trim a side twice")

    assert len(callers) >= 3, (
        f"expected the known entry points into team creation, found {callers}")


def test_the_queue_explainer_does_not_describe_the_retired_matcher():
    """The embed makes falsifiable claims about level_side, and prose cannot be
    typechecked. This is the cheapest anchor: the vocabulary of the retired
    tiered matcher must not reappear in the text a player reads.

    It caught nothing when written -- the rewrite came first. It exists because
    the explainer and the rule live in different files, and the last time they
    drifted the embed described a system that had not run for months.

    Two later changes proved the anchor is worth widening rather than merely
    keeping. The cap moved from reading the OPPOSING side to reading your own,
    and the copy was renamed off "bet" entirely; neither retirement was listed
    here, so nothing failed when the explainer went on describing the old rule.
    A reviewer found that instead, which is precisely the job this test exists
    to do.

    "opponent" is deliberately NOT banned: the live copy says the cap depends on
    your team and "not your opponents", which is the correct statement of the new
    rule. What is banned is the vocabulary that can only belong to the old one.
    """
    import inspect
    import re

    from views import PersistentView

    text = inspect.getsource(PersistentView.explain_stakes_callback).lower()

    for retired in ("proportional", "tiered", "minimum requirement",
                    "player-to-player", "betting pair", "bet score",
                    # the cap used to measure against the other side
                    "opposing", "biggest opponent", "highest opponent",
                    # and used to snap to a fixed ladder, then to a median
                    "bucket", "median"):
        assert retired not in text, (
            f"the queue explainer still uses {retired!r}, which belongs to a "
            f"retired rule, not the one the pool runs today")

    # Whole words only: "bet" is a substring of "between" and "better", either of
    # which a future edit could legitimately introduce.
    stale = re.findall(r"\b(bet|bets|betting|bettor|better)\b", text)
    assert not stale, (
        f"the queue explainer still calls it a bet ({sorted(set(stale))}); the "
        "player-facing term is an entry")


# ---- entry capping: the 🧢 preference, applied before levelling --------------------
#
# "Cap my entry so I never carry more than my share of my own team" is a personal
# ceiling a player opts into at signup. It runs BEFORE levelling and hands the
# excess straight back, so a capped player never has money sitting in a pot they
# said they did not want that much action in.
#
# Measured against their own side rather than the opponents, because that is what
# the history says matters: a side whose top entry holds 55-60% of it wins 44.5%
# and 65%+ wins 42.2%, against 50% overall -- while being merely the biggest
# entry is harmless at 49.7%.


async def _a_400_on_a_side_of_20(b1_capped):
    """The scenario the cap tests turn on: b1 declares 400 alongside one
    teammate on 20, so b1 carries 95% of their own side. Only b1's own
    preference varies between them."""
    await _fund({"a1": 100, "a2": 100, "b1": 400, "b2": 20})
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False),
                             "b1": (400, b1_capped), "b2": (20, False)})


@pytest.mark.asyncio
async def test_a_capped_entry_is_trimmed_to_its_share_of_its_own_side(test_db):
    """400 beside a single teammate on 20 comes back to 20.

    CAP_SHARE of 55% allows 1.222x the teammates' total -- 24 here -- and the
    ceiling snaps DOWN to a whole stake step, so 20.
    """
    await _a_400_on_a_side_of_20(b1_capped=True)

    result = await pool.match_pool("g", "s1", A, B)

    assert result["capped"] == {"b1": 380}, (
        f"the capped entry was not trimmed to its share of its side: {result}")
    held = await pool.contributions("g", "s1")
    assert held["b1"] == 20, f"the cap did not stick: {held}"


@pytest.mark.asyncio
async def test_an_uncapped_entry_is_left_for_levelling(test_db):
    """🏎️ means the entry is not trimmed up front; it takes its chances with
    the ceiling like everyone else, and keeps whatever the other side covers."""
    await _a_400_on_a_side_of_20(b1_capped=False)

    result = await pool.match_pool("g", "s1", A, B)

    assert result["capped"] == {}, f"an uncapped entry was trimmed: {result}"
    held = await pool.contributions("g", "s1")
    assert held["b1"] == 180, (
        f"the uncapped entry did not take what the other side could cover: {held}")


@pytest.mark.asyncio
async def test_the_modal_roster_is_left_alone(test_db):
    """100 beside two teammates on 50 is the single most common shape in the
    history -- 164 occurrences -- and it wins 52.4%, so it is not what the cap
    is for. It sits at exactly 50% of its side, which is why the threshold is
    55% and not 50%: a 50% ceiling would clip the modal roster.
    """
    await _fund({"a1": 100, "a2": 100, "a3": 100,
                 "b1": 100, "b2": 50, "b3": 50})
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False), "a3": (100, False),
                             "b1": (100, True), "b2": (50, True), "b3": (50, True)})

    result = await pool.match_pool("g", "s1", ["a1", "a2", "a3"], ["b1", "b2", "b3"])

    assert result["capped"] == {}, f"the modal roster was clipped: {result}"


@pytest.mark.asyncio
async def test_an_entry_over_the_share_is_trimmed_even_on_a_full_team(test_db):
    """100 beside 20 and 50 is 59% of its side, and that band wins 44.5%.
    Teammates total 70, so the allowance is 85 and the ceiling snaps to 80 --
    leaving b1 at 53% of the side, which is what the 55% rule asks for.

    The snap direction is load-bearing and this is the case that shows it: an
    allowance is rarely a whole step, and rounding 85 UP to 90 would leave b1
    holding 90 of 160, or 56% -- past the line the cap exists to hold. Down,
    never up.
    """
    await _fund({"a1": 100, "a2": 100, "a3": 100,
                 "b1": 100, "b2": 20, "b3": 50})
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False), "a3": (100, False),
                             "b1": (100, True), "b2": (20, False), "b3": (50, False)})

    result = await pool.match_pool("g", "s1", ["a1", "a2", "a3"], ["b1", "b2", "b3"])

    assert result["capped"] == {"b1": 20}, (
        f"an entry at 59% of its side was not trimmed to its allowance: {result}")


@pytest.mark.asyncio
async def test_the_allowance_grows_with_the_team(test_db):
    """The share is of the whole side, so how MANY teammates you have matters --
    which is what a ceiling read from one teammate's figure cannot express.
    Three teammates on 20 total 60, allowing 73, so 50 stands where beside a
    single 20 it would not."""
    await _fund({"a1": 100, "a2": 100, "a3": 100, "a4": 100,
                 "b1": 50, "b2": 20, "b3": 20, "b4": 20})
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False),
                             "a3": (100, False), "a4": (100, False),
                             "b1": (50, True), "b2": (20, False),
                             "b3": (20, False), "b4": (20, False)})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["capped"] == {}, (
        f"50 beside three 20s is 45% of its side and should stand: {result}")


@pytest.mark.asyncio
async def test_a_capped_entry_under_the_share_is_untouched(test_db):
    """The cap only ever trims. Opting in cannot cost a player already inside
    their share."""
    await _fund({"a1": 100, "a2": 100, "b1": 50, "b2": 50})
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False),
                    "b1": (50, True), "b2": (50, True)})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["capped"] == {}, f"an entry below the cap was trimmed: {result}"


@pytest.mark.asyncio
async def test_a_lone_entry_with_no_teammates_is_never_capped(test_db):
    """Nobody to take a share OF. Treating an empty teammate total as a ceiling
    of zero would refund their whole entry."""
    await _fund({"a1": 100, "b1": 100})
    await seed_stakes("s1", {"a1": (100, False), "b1": (100, True)})

    result = await pool.match_pool("g", "s1", ["a1"], ["b1"])

    assert result["capped"] == {}, f"a lone entry was capped to nothing: {result}"
    held = await pool.contributions("g", "s1")
    assert held["b1"] == 100, held


@pytest.mark.asyncio
async def test_capping_does_not_ratchet_when_matching_is_replayed(test_db):
    """The regression the declared-entry key exists to prevent.

    team_creator replays match_pool after a restart. A ceiling read from what
    the side currently HOLDS would fall on every pass -- levelling shrinks the
    teammates' holdings, so the second call would compute a smaller allowance,
    trim again, and shrink the pot on every replay. Reading StakeInfo.max_stake,
    which levelling never touches, makes the ceiling the same figure every time.
    """
    await _a_400_on_a_side_of_20(b1_capped=True)

    first = await pool.match_pool("g", "s1", A, B)
    after_one = await pool.contributions("g", "s1")
    second = await pool.match_pool("g", "s1", A, B)
    after_two = await pool.contributions("g", "s1")

    assert second["capped"] == {}, f"the cap was charged twice: {second}"
    assert second["matched"] == first["matched"], (
        f"the pot shrank on a replay: {first['matched']} -> {second['matched']}")
    assert after_two == after_one, f"a replay moved money: {after_one} -> {after_two}"


@pytest.mark.asyncio
async def test_a_draft_with_no_declared_bets_is_never_capped(test_db):
    """A premade entry fee has no StakeInfo and nothing to take a share of.
    Absent rows must read as "no preference", not as a ceiling of zero -- which
    would refund the whole table."""
    await _fund({"a1": 100, "a2": 100, "b1": 100, "b2": 100})

    result = await pool.match_pool("g", "s1", A, B)

    assert result["capped"] == {}, f"a fee draft was capped: {result}"
    held = await pool.contributions("g", "s1")
    assert all(held[p] == 100 for p in ("a1", "a2", "b1", "b2")), held


# ---- max_pool: what the queue can honestly advertise -------------------------------
#
# The queue shows a ceiling before teams exist, so it cannot ask match_pool --
# that needs the split. What it CAN say is how big the pot would be if the
# sign-ups paired off as favourably as they could.
#
# This is the POT, not one side of it: when a 200 meets a 100 they meet at 100,
# and both put that in, so 200 is played for. A winner takes their own back plus
# an opponent's, which is why the pot is what "prize pool" names.


def test_a_lone_bet_has_only_its_own_money_in_the_pot():
    """First in the queue: 200 is in, and the joiner who covers it brings theirs
    when they arrive. Until then the pot holds one player's money."""
    assert pool.max_pool([200]) == 200


def test_two_bets_that_meet_at_the_smaller_are_both_played_for():
    """200 against 100 meets at 100 a side -- so 200 is the pot, not 300."""
    assert pool.max_pool([200, 100]) == 200


def test_a_roster_that_wastes_nothing_plays_for_every_tix():
    """Two 200s and two 100s split evenly: nothing is unmatched, so the pot is
    the whole sum."""
    assert pool.max_pool([200, 200, 100, 100]) == 600


def test_the_odd_player_out_adds_only_their_own_stake():
    assert pool.max_pool([200, 100, 50]) == 250


def test_the_pot_is_quoted_in_whole_tens():
    """Matching happens in tens, so a 25 backs 20 and the pair is worth 40."""
    assert pool.max_pool([25, 200]) == 40


def test_an_empty_queue_has_no_pool():
    assert pool.max_pool([]) == 0


@pytest.mark.asyncio
async def test_the_advertised_pot_is_the_pot_the_draft_actually_holds(test_db):
    """The queue's promise and the fire's arithmetic must not drift apart.

    max_pool is a claim about what the holder will contain once teams exist and
    match_pool has refunded the unmatched. Nothing enforces that but this test.

    The roster is deliberately lopsided: a symmetric one cannot catch a pairing
    that takes the LARGER of each pair, because both readings agree there -- so
    it would be a test that passes on broken code.
    """
    stakes = {"a1": 200, "a2": 100, "b1": 100, "b2": 100}
    await _fund(stakes)

    advertised = pool.max_pool(stakes.values())
    assert advertised == 400, "the 200 can only be backed by the 100 facing it"

    # No split does better: the side holding the 200 always outweighs the other.
    result = await pool.match_pool("g", "s1", ["a1", "a2"], ["b1", "b2"])

    assert await pool.pool_balance("g", "s1") == advertised, (
        f"queue advertised {advertised}, holder actually has "
        f"{await pool.pool_balance('g', 's1')}")
    assert result["matched"] * 2 == advertised, "the pot is both sides, not one"
    assert sum(result["refunded"].values()) == 100, (
        f"the unmatchable 100 should have gone back: {result['refunded']}")


def test_several_small_bets_together_can_back_one_big_one():
    """match_pool compares SIDE TOTALS, not player against player.

    100 and 20 on one team face 50 and 50 on the other: the sides meet at 100,
    so 200 is played for. A model that pairs entries off one-to-one -- largest
    against second largest -- says 140, because it never lets the two 50s add
    up. Found by review; the queue was under-advertising drafts it could pay.
    """
    assert pool.max_pool([100, 20, 50, 50]) == 200


def _best_pot_by_brute_force(stakes: list[int]) -> int:
    """Oracle: try every legal split and take the biggest pot any of them makes.

    Legal means equal-size teams, which is what team_creator enforces. After
    match_pool the holder holds 2 x the smaller side's whole-ten total, so that
    is the pot a given split produces.
    """
    from itertools import combinations
    floored = [n // 10 * 10 for n in stakes]
    n = len(floored)
    if n % 2:
        return 0        # cannot fire; no split to maximise over
    best = 0
    for idx in combinations(range(n), n // 2):
        a = sum(floored[i] for i in idx)
        b = sum(floored[i] for i in range(n) if i not in idx)
        best = max(best, 2 * min(a, b))
    return best


@pytest.mark.asyncio
async def test_the_advertised_pot_is_an_UPPER_bound_once_entries_are_capped(test_db):
    """The board's figure is pre-cap, and an opted-in cap can only lower it.

    Every other max_pool test seeds entries through _fund, which never writes
    is_capped -- so they all exercise the UNCAPPED path and none of them can see
    a cap change move the achievable pot. That blind spot is what let the cap
    move from reading the opposing side to reading your own without a single
    failure: measured on the prod copy, 22 of 917 real queues (2.4%) can no
    longer reach their advertised figure, worst case 900 advertised against 320
    achievable.

    So the contract is stated here as the inequality it actually is. Exactness
    is not merely unimplemented, it is unattainable at the point the board
    renders: the ceiling depends on who you are drawn WITH, and the cap flags of
    players who have not joined yet are unknowable. "Up to N" is the honest
    claim, and this pins that it stays an upper bound rather than drifting into
    a figure the draft cannot pay.
    """
    stakes = {"a1": 100, "a2": 20, "b1": 100, "b2": 20}
    await _fund(stakes)
    await seed_stakes("s1", {p: (n, True) for p, n in stakes.items()})

    advertised = pool.max_pool(stakes.values())
    assert advertised == 240, "pre-cap, the two sides can meet at 120 each"

    result = await pool.match_pool("g", "s1", ["a1", "a2"], ["b1", "b2"])
    actual = await pool.pool_balance("g", "s1")

    assert actual <= advertised, (
        f"the queue advertised {advertised} and the draft holds {actual} -- "
        "the board must never promise more than a draft can pay")
    assert actual == 80, (
        f"each 100 sits beside a single 20, so its ceiling snaps to 20: {result}")
    assert result["matched"] * 2 == actual


@pytest.mark.parametrize("stakes", [
    [200, 100],
    [100, 20, 50, 50],
    [200, 200, 100, 100],
    [20, 20, 200, 200],
    [25, 200],
    [50] * 8,
    [20, 170, 200, 60, 50, 110],
    [10, 10, 10, 500],
    [90, 80, 70, 60, 50, 40, 30, 20],
    [200, 20, 20, 20, 20, 20, 20, 20],
])
def test_the_advertised_pot_is_the_best_any_legal_split_could_make(stakes):
    """The ceiling must be exactly reachable -- not above (a promise the draft
    cannot keep) and not below (a draft that pays more than it advertised).

    Exactly reachable BEFORE any entry cap applies, which is what max_pool
    models and all this oracle knows about. A capped entry can lower the
    achievable pot below this figure; that case is
    test_the_advertised_pot_is_an_UPPER_bound_once_entries_are_capped.
    """
    assert pool.max_pool(stakes) == _best_pot_by_brute_force(stakes)


@pytest.mark.asyncio
async def test_a_ceiling_that_snaps_to_nothing_never_refunds_a_whole_entry(test_db):
    """The guard has to test the CEILING, not the teammates' total.

    Teammates totalling under 9 give an allowance under one step, snapping to 0 --
    and a ceiling of zero would hand back the capped player's entire entry, the
    one outcome this must never do. Guarding on the teammate total instead lets
    that through, because the total is positive.
    """
    from services.draft_pool_service import cap_targets

    targets = cap_targets(["x", "y"], {"x": 200, "y": 5}, {"x"},
                          {"x": 200, "y": 5})

    assert targets["x"] == 200, (
        f"a ceiling that snapped to nothing refunded the whole entry: {targets}")
