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

    assert await stake_funding.shortfall("g", P, "new", 100) == 0


@pytest.mark.asyncio
async def test_an_underfunded_player_is_told_exactly_what_they_need(test_db):
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 60, "seed", "t")

    assert await stake_funding.shortfall("g", P, "new", 100) == 40


@pytest.mark.asyncio
async def test_an_existing_debt_must_be_covered_first(test_db):
    """Outstanding obligations come before new ones."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")
    await _owes("g", P, "creditor", 60)

    assert await stake_funding.shortfall("g", P, "new", 50) == 10


@pytest.mark.asyncio
async def test_being_owed_money_does_not_pay_for_a_stake(test_db):
    """A receivable is not cash, and you cannot pay Alice with Bob's IOU."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")
    await _owes("g", P, "alice", 60)     # p1 owes alice 60
    await _owes("g", "bob", P, 60)       # bob owes p1 60

    assert await stake_funding.shortfall("g", P, "new", 50) == 10, (
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

    assert await stake_funding.shortfall("g", P, "new", 50) == 10


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

    assert await stake_funding.shortfall("g", P, "new", 50) == 10, (
        f"a draft at stage {stage!r} stopped counting against the wallet")


@pytest.mark.asyncio
async def test_raising_a_stake_in_the_same_draft_does_not_double_count(test_db):
    """Changing 60 to 80 needs 80, not 140."""
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await _declare("new", P, 60)
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert await stake_funding.shortfall("g", P, "new", 80) == 0


@pytest.mark.asyncio
async def test_a_finished_draft_is_not_a_potential_obligation(test_db):
    """Its risk is resolved; whatever it cost is already in the debt figure."""
    await seed_session("done", guild="g", stype="staked", stage="completed",
                       sign_ups={P: "Ada"})
    await _declare("done", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert await stake_funding.shortfall("g", P, "new", 100) == 0


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

    assert await stake_funding.shortfall("g", P, "new", 100) == 0


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_not_a_potential_obligation(test_db):
    await seed_session("gone", guild="g", stype="staked", stage="abandoned",
                       sign_ups={P: "Ada"})
    await _declare("gone", P, 60)
    await seed_session("new", guild="g", stype="staked", stage=None,
                       sign_ups={P: "Ada"})
    await wallet_service.adjust("g", P, 100, "seed", "t")

    assert await stake_funding.shortfall("g", P, "new", 100) == 0


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
