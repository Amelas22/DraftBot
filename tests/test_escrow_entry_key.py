"""The entry fee's idempotency key must survive a drop.

tournament_participants.id is a plain SQLite rowid alias (no AUTOINCREMENT), so
remove_team frees the dropped team's id and the next registrant inherits it. A
participant-keyed escrow source therefore collided with the dropped team's
already-booked transfer, seating the newcomer without charging them. The key is
keyed on the persistent Team identity instead; these pin that down, and pin down
that entries booked under the OLD key still refund.
"""
import random

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant
from services import tournament_escrow_service as escrow
from services import tournament_service as tsvc
from services import wallet_service

GUILD = "g-repro"
CAP_A = "cap-aaa"
CAP_B = "cap-bbb"


async def _tournament(fee=150):
    async with db_session() as s:
        t = Tournament(guild_id=GUILD, name="Repro Cup", total_rounds=3, format="swiss",
                       status="registration", entry_fee=fee)
        s.add(t)
        await s.flush()
        return t.id


@pytest.mark.asyncio
async def test_a_reused_participant_id_still_charges_the_new_team(test_db):  # noqa: F811
    t_id = await _tournament()

    async with db_session() as s:
        a, _ = await tsvc.register_team(s, t_id, "Team Alpha", CAP_A)
        a_id = a.id
    await wallet_service.credit_done(GUILD, CAP_A, 150, job_id="j-a")
    await escrow.sweep_pending_entries()
    assert await escrow.prize_pool(GUILD, t_id) == 150

    await escrow.drop_with_refund(t_id, "Team Alpha")

    async with db_session() as s:
        b, _ = await tsvc.register_team(s, t_id, "Team Beta", CAP_B)
        b_id = b.id

    # The premise of the whole test: SQLite handed Beta the id Alpha just freed.
    # If this ever stops holding the test still passes, but for the wrong reason.
    assert b_id == a_id, "participant id was not reused; the collision can't occur"

    # Beta funds their wallet and the sweep runs. If the id was reused, the escrow
    # source 'tourney:<t>:<id>' already has a booked transfer from Alpha.
    await wallet_service.credit_done(GUILD, CAP_B, 150, job_id="j-b")
    await escrow.sweep_pending_entries()

    async with db_session() as s:
        beta = await s.get(TournamentParticipant, b_id)
        beta_status = beta.status
    beta_balance = await wallet_service.get_balance(GUILD, CAP_B)
    pot = await escrow.prize_pool(GUILD, t_id)

    # What SHOULD hold: Beta paid 150, so their wallet is empty and the pot holds it.
    assert beta_status == "paid"
    assert beta_balance == 0, f"Beta was marked paid without being charged (wallet {beta_balance})"
    assert pot == 150, f"pot should hold Beta's fee, holds {pot}"


@pytest.mark.asyncio
async def test_an_entry_booked_under_the_legacy_key_still_refunds(test_db):  # noqa: F811
    """Entries paid before the key changed are keyed by participant id. Dropping one
    must still find its leg and return the fee — otherwise the change silently strands
    every fee already in a live tournament's pot."""
    t_id = await _tournament()
    async with db_session() as s:
        p, _ = await tsvc.register_team(s, t_id, "Legacy Squad", CAP_A)
        p_id = p.id

    # Book the fee exactly as the old code did: source = tourney:<t>:<participant_id>.
    await wallet_service.credit_done(GUILD, CAP_A, 150, job_id="j-legacy")
    async with db_session() as s:
        legacy = escrow.escrow_source(t_id, p_id)
        await wallet_service.transfer_in(
            s, GUILD, CAP_A, wallet_service.prize_wallet_id(t_id), 150, legacy,
            notes="tournament entry: Legacy Squad")
        (await s.get(TournamentParticipant, p_id)).status = "paid"
    assert await escrow.prize_pool(GUILD, t_id) == 150

    res = await escrow.drop_with_refund(t_id, "Legacy Squad")

    assert res["refunded"] == 150, "a legacy-keyed entry was not refunded"
    assert await wallet_service.get_balance(GUILD, CAP_A) == 150
    assert await escrow.prize_pool(GUILD, t_id) == 0


@pytest.mark.asyncio
async def test_a_legacy_paid_entry_is_not_charged_twice_by_a_later_sweep(test_db):  # noqa: F811
    """The 'already booked?' probe has to recognise the legacy key too, or the next
    sweep bills a captain who has already paid."""
    t_id = await _tournament()
    async with db_session() as s:
        p, _ = await tsvc.register_team(s, t_id, "Legacy Two", CAP_A)
        p_id = p.id
    await wallet_service.credit_done(GUILD, CAP_A, 300, job_id="j-legacy2")
    async with db_session() as s:
        await wallet_service.transfer_in(
            s, GUILD, CAP_A, wallet_service.prize_wallet_id(t_id), 150,
            escrow.escrow_source(t_id, p_id), notes="tournament entry: Legacy Two")

    # Participant is still 'pending', so the sweep will look at it again.
    await escrow.sweep_pending_entries()

    assert await wallet_service.get_balance(GUILD, CAP_A) == 150, "captain was billed twice"
    assert await escrow.prize_pool(GUILD, t_id) == 150


@pytest.mark.asyncio
async def test_a_team_that_drops_and_re_registers_pays_again(test_db):  # noqa: F811
    """A refund does not delete the entry leg — it books a compensating one. So the
    'has this team paid?' probe must ask whether the entry is still STANDING, not
    whether it ever existed, or a team can drop, be refunded, re-register under the
    same Team identity and be seated for free."""
    t_id = await _tournament()
    async with db_session() as s:
        await tsvc.register_team(s, t_id, "Boomerang", CAP_A)
    await wallet_service.credit_done(GUILD, CAP_A, 150, job_id="j-boom")
    await escrow.sweep_pending_entries()
    assert await escrow.prize_pool(GUILD, t_id) == 150

    await escrow.drop_with_refund(t_id, "Boomerang")
    assert await wallet_service.get_balance(GUILD, CAP_A) == 150
    assert await escrow.prize_pool(GUILD, t_id) == 0

    # Same team name -> same persistent Team row -> same team_id as before.
    async with db_session() as s:
        again, _ = await tsvc.register_team(s, t_id, "Boomerang", CAP_A)
        again_id = again.id
    await escrow.sweep_pending_entries()

    async with db_session() as s:
        status = (await s.get(TournamentParticipant, again_id)).status
    assert status == "paid"
    assert await wallet_service.get_balance(GUILD, CAP_A) == 0, "re-entry was not charged"
    assert await escrow.prize_pool(GUILD, t_id) == 150, "pot did not receive the re-entry fee"


@pytest.mark.asyncio
async def test_a_refunded_legacy_entry_does_not_read_as_paid(test_db):  # noqa: F811
    """The netting has to apply to the legacy key too. Otherwise a team paid under the
    old key, refunded, then re-registered onto the freed participant id would be seated
    free — the original bug, one branch lower."""
    t_id = await _tournament()
    async with db_session() as s:
        p, _ = await tsvc.register_team(s, t_id, "Old Key", CAP_A)
        p_id = p.id
    await wallet_service.credit_done(GUILD, CAP_A, 300, job_id="j-oldkey")
    legacy = escrow.escrow_source(t_id, p_id)
    async with db_session() as s:
        # Paid under the legacy key, then refunded — both legs stay in the ledger.
        await wallet_service.transfer_in(
            s, GUILD, CAP_A, wallet_service.prize_wallet_id(t_id), 150, legacy,
            notes="tournament entry: Old Key")
        await wallet_service.transfer_in(
            s, GUILD, wallet_service.prize_wallet_id(t_id), CAP_A, 150, f"refund:{legacy}",
            notes="entry refund: Old Key")
    assert await escrow.prize_pool(GUILD, t_id) == 0

    await escrow.sweep_pending_entries()

    async with db_session() as s:
        assert (await s.get(TournamentParticipant, p_id)).status == "paid"
    assert await wallet_service.get_balance(GUILD, CAP_A) == 150, "the re-entry was not charged"
    assert await escrow.prize_pool(GUILD, t_id) == 150


@pytest.mark.asyncio
async def test_the_unpaid_refusal_stays_inside_discords_message_limit(test_db):  # noqa: F811
    """Every unpaid name in one message is unbounded; Discord caps content at 2000."""
    t_id = await _tournament()
    async with db_session() as s:
        for i in range(60):
            await tsvc.register_team(s, t_id, f"A Team With A Fairly Long Name {i:02d}", f"cap{i}")

    with pytest.raises(ValueError) as err:
        await escrow.close_registration_and_seed(GUILD, random.Random(1))

    message = str(err.value)
    assert len(message) < 2000, f"refusal is {len(message)} chars"
    assert "60 teams haven't paid" in message
    assert "and 40 more" in message      # 20 named, the rest counted
