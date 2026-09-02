"""Nobody takes on a draft's risk they cannot cover.

The invariant, checked at the moment a stake is declared:

    balance >= what you already owe
             + the most you could lose from drafts you are already in
             + the stake you are declaring now

It is a precondition on taking on risk, not a lock on the wallet. That is the
whole point of doing it first: once every player in a draft is known solvent,
moving to real escrow cannot strand anyone, and until then the legacy debt path
keeps working untouched.
"""
import pytest
import pytest_asyncio
from sqlalchemy import update

from conftest import seed_session
from models.draft_session import DraftSession
from models.stake import StakeInfo
from services import stake_funding
from services import wallet_service
from session import AsyncSessionLocal

P = "p1"


async def _declare(session_id, player, amount, guild="g"):
    """A stake declared on a draft the player is signed up for."""
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(StakeInfo(session_id=session_id, player_id=player,
                             max_stake=amount, is_capped=False))


async def _owes(guild, debtor, creditor, amount):
    """Book a debt the way a lost draft books one: mirrored rows."""
    from models.debt_ledger import DebtLedger

    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(DebtLedger(guild_id=guild, player_id=debtor,
                              counterparty_id=creditor, amount=-amount,
                              source_type="draft", source_id="old-draft"))
            db.add(DebtLedger(guild_id=guild, player_id=creditor,
                              counterparty_id=debtor, amount=amount,
                              source_type="draft", source_id="old-draft"))


@pytest.mark.asyncio
async def test_a_funded_player_with_no_history_can_declare(test_db):
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 0


@pytest.mark.asyncio
async def test_an_underfunded_player_is_told_exactly_what_they_need(test_db):
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 60, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 40


@pytest.mark.asyncio
async def test_an_existing_debt_must_be_covered_first(test_db):
    """Outstanding obligations come before new ones."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")
    await _owes("g", P, "creditor", 60)

    assert (await stake_funding.shortfall("g", P, "new", 50))["gap"] == 10


@pytest.mark.asyncio
async def test_being_owed_money_does_not_pay_for_a_stake(test_db):
    """A receivable is not cash, and you cannot pay Alice with Bob's IOU."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")
    await _owes("g", P, "alice", 60)     # p1 owes alice 60
    await _owes("g", "bob", P, 60)       # bob owes p1 60

    assert (await stake_funding.shortfall("g", P, "new", 50))["gap"] == 10, (
        "a debt owed TO this player was allowed to offset one they owe")


@pytest.mark.asyncio
async def test_a_draft_they_are_already_in_is_a_potential_obligation(test_db):
    """The most they could lose elsewhere is money they cannot stake here."""
    await seed_session("other", guild="g", stype="staked", stage="teams",
                       sign_ups={P: "Ada"})
    await _declare("other", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 50))["gap"] == 10


@pytest.mark.parametrize("stage", [None, "teams", "pairings"])
@pytest.mark.asyncio
async def test_a_draft_at_any_unfinished_stage_still_holds_its_stake(test_db, stage):
    """Queued, teams formed, or being played -- the money is equally committed.

    'pairings' is the long-lived one: a draft spends minutes in the queue and
    an hour being played, so it is the state a second signup is most likely to
    collide with.
    """
    await seed_session("other", guild="g", stype="staked", stage=stage,
                       sign_ups={P: "Ada"})
    await _declare("other", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 50))["gap"] == 10, (
        f"a draft at stage {stage!r} stopped counting against the wallet")


@pytest.mark.asyncio
async def test_raising_a_stake_in_the_same_draft_does_not_double_count(test_db):
    """Changing 60 to 80 needs 80, not 140."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await _declare("new", P, 60)
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 80))["gap"] == 0


@pytest.mark.asyncio
async def test_a_finished_draft_is_not_a_potential_obligation(test_db):
    """Its risk is resolved; whatever it cost is already in the debt figure."""
    await seed_session("done", guild="g", stype="staked", stage="completed",
                       sign_ups={P: "Ada"})
    await _declare("done", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 0


@pytest.mark.asyncio
async def test_a_draft_they_left_is_not_a_potential_obligation(test_db):
    """Leaving does not delete the StakeInfo row, so row-existence alone would
    hold a player's money hostage to a draft they walked away from."""
    await seed_session("left", guild="g", stype="staked", stage=None,
                       sign_ups={"someone_else": "Brin"})
    await _declare("left", P, 60)     # the row they left behind
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 0


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_not_a_potential_obligation(test_db):
    await seed_session("gone", guild="g", stype="staked", stage="abandoned",
                       sign_ups={P: "Ada"})
    await _declare("gone", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 0


def test_every_stake_declaration_passes_through_the_gate():
    """Three UI paths declare a stake, and a gate on two of them is a gate on
    none: a player refused at the dropdown can raise the same amount through
    the "Over 100 TIX" modal or the combined settings panel.
    """
    import ast
    import inspect
    import textwrap

    import views

    handlers = [
        ("StakeOptionsSelect", views.StakeOptionsSelect.handle_stake_submission),
        ("StakeModal", views.StakeModal.callback),
        ("CombinedStakeSelect", views.CombinedStakeSelect.handle_stake_submission),
    ]
    for name, fn in handlers:
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
        gated = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "refuse_unfunded_stake"]
        assert gated, f"{name} declares a stake without checking the wallet"

        writes = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and ((isinstance(n.func, ast.Name) and n.func.id == "StakeInfo")
                       or (isinstance(n.func, ast.Attribute)
                           and n.func.attr in ("add", "record_signup_event")))]
        if writes:
            assert min(n.lineno for n in gated) < min(writes), (
                f"{name} writes the sign-up before checking the wallet, so a "
                "refused player is already in the draft")


@pytest.mark.asyncio
async def test_the_refusal_tells_the_player_what_to_deposit(test_db):
    """"No" is not actionable; the gap is."""
    from unittest.mock import AsyncMock, MagicMock

    from views import refuse_unfunded_stake

    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 40, "seed", "t")
    await _owes("g", P, "creditor", 25)

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    refused = await refuse_unfunded_stake(interaction, "new", "g", P, 100)

    assert refused is True
    said = interaction.response.send_message.call_args.args[0]
    assert "85 more tix" in said, said        # 25 owed + 100 stake - 40 held
    assert "25 tix of debt" in said, said
    assert "40" in said, said


@pytest.mark.asyncio
async def test_an_entry_already_escrowed_is_not_counted_twice(test_db):
    """Paying the entry satisfies the obligation it stood for.

    The tix are no longer promised, they are delivered, and the balance already
    reflects that. Treating the declared stake as still outstanding would ask
    the player to make good on it twice: someone with 100 who put 50 into one
    draft would be refused a 50 stake in the next, holding exactly enough.
    """
    from services import draft_pool_service as pool

    await seed_session("paid", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    await pool.set_entry("g", "paid", P, 50)      # 50 leaves the wallet
    await _declare("paid", P, 50)                 # and is declared alongside it

    assert await wallet_service.get_balance("g", P) == 50
    assert (await stake_funding.shortfall("g", P, "new", 50))["gap"] == 0, (
        "an obligation already satisfied by escrow is still being demanded")


@pytest.mark.asyncio
async def test_raising_a_stake_only_needs_the_difference(test_db):
    """The entry already paid into THIS draft counts toward the new figure.

    Escrow makes revising a stake a top-up, not a fresh purchase: a player 50
    into a draft who raises to 100 owes the pool 50, not 100. potential_losses
    already treats a paid entry as a satisfied obligation, but it excludes the
    draft being revised, so that credit was never applied to the one draft it
    mattered most for -- and the player was asked to fund their whole stake a
    second time out of a wallet the first payment had just emptied.
    """
    from services import draft_pool_service as pool

    await seed_session("cur", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 60, "seed", "t")
    await pool.set_entry("g", "cur", P, 50)
    await _declare("cur", P, 50)

    assert await wallet_service.get_balance("g", P) == 10
    assert await pool.held_by("g", "cur", P) == 50

    # 100 total, 50 already in, 10 in the wallet -> 40 short.
    assert (await stake_funding.shortfall("g", P, "cur", 100))["gap"] == 40
    # 90 total, 50 already in, 10 in the wallet -> 30 short.
    assert (await stake_funding.shortfall("g", P, "cur", 90))["gap"] == 30
    # Lowering, or holding steady, needs nothing.
    assert (await stake_funding.shortfall("g", P, "cur", 50))["gap"] == 0
    assert (await stake_funding.shortfall("g", P, "cur", 20))["gap"] == 0


@pytest.mark.asyncio
async def test_a_stake_levelled_down_stops_reserving_the_refunded_part(test_db):
    """Once the book closes, a declaration is no longer a claim on the wallet.

    Matching hands back whatever could not be matched, so a player who declared
    100 and was levelled to 50 has 50 at risk and 50 back in their wallet. They
    owe that draft nothing further. Reserving max_stake minus what is held
    would count the returned 50 a second time and refuse them a stake they can
    plainly afford.
    """
    from sqlalchemy import update

    from services import draft_pool_service as pool
    from session import DraftSession

    await seed_session("levelled", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")
    await pool.set_entry("g", "levelled", P, 100)
    await _declare("levelled", P, 100)
    # Matching returns half and closes the book.
    await pool.refund_entry("g", "levelled", P, 50, "unmatched")
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "levelled")
                             .values(session_stage="teams"))

    assert await wallet_service.get_balance("g", P) == 50
    owed, at_risk = await stake_funding.obligations("g", P)
    assert at_risk == 0, (
        f"the draft still reserves {at_risk} tix, but its entry is paid and "
        "the unmatched part is already back in the wallet")


@pytest.mark.asyncio
async def test_a_played_out_draft_left_at_pairings_is_not_a_potential_obligation(test_db):
    """A posted victory message finishes a draft, whatever the stage says.

    session_stage is not a reliable record of completion -- helpers/stale_drafts
    says so in as many words ("the stage rarely advances past 'pairings' even for
    fully played drafts"), and nothing wrote 'completed' AT ALL before 2026-01, so
    every staked draft older than that still reads as live. Trusting the stage
    alone held 410 tix of a real player's wallet hostage to drafts that had
    finished up to seventeen months earlier.
    """
    await seed_session("played", guild="g", stype="staked", stage="pairings",
                       victory=12345, sign_ups={P: "Ada"})
    await _declare("played", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert (await stake_funding.shortfall("g", P, "new", 100))["gap"] == 0
