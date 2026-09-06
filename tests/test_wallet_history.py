"""What a wallet ledger row was FOR.

Every writer sets a structured `source` -- it is the transfer pair's idempotency
key, so it is well-formed and unique per event. These tests pin the mapping from
that key to something a player can read.
"""
from datetime import datetime

import pytest

from conftest import seed_session, test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import Tournament
from models.wallet_tx import WalletTx
from services import wallet_history as wh
from services import wallet_service as ws


def _tx(kind="pay", amount=-10, source=None, counterparty_id=None, notes=None):
    """An unattached ledger row. classify() is pure, so no database is needed."""
    return WalletTx(kind=kind, amount=amount, source=source,
                    counterparty_id=counterparty_id, notes=notes)


def _withdrawal_and_its_return():
    """The two legs of a withdrawal that MTGO rejected: the tix committed to the
    in-flight holder, and the same tix booked straight back by
    mtgo_resolution_service's _return_in_flight. Both legs name that holder, so
    telling them apart is the display's job."""
    return (_tx(kind="pay", amount=-50, source="wd:commit-1",
                counterparty_id=ws.SYSTEM_IN_FLIGHT),
            _tx(kind="receive", amount=50, source="return:wd:commit-1",
                counterparty_id=ws.SYSTEM_IN_FLIGHT))


@pytest.mark.parametrize("source, kind, category, event, ref", [
    ("draft-entry:sid1:p1:0:0-10", "pay", wh.DRAFT, "entry", "sid1"),
    ("draft-payout:sid1:p1", "receive", wh.DRAFT, "winnings", "sid1"),
    ("draft-refund:cancelled:sid1:p1:0", "receive", wh.DRAFT, "refund", "sid1"),
    ("tourney:3:18", "pay", wh.TOURNAMENT, "entry", "3"),
    ("tourney:3:18:1", "pay", wh.TOURNAMENT, "entry", "3"),
    ("refund:tourney:3:18", "receive", wh.TOURNAMENT, "refund", "3"),
    ("payout:3:1", "receive", wh.TOURNAMENT, "prize", "3"),
    ("debt:93c531db", "receive", wh.DEBT, "settled", None),
    ("serve", "deposit", wh.MTGO, "deposit", None),
    ("wd:commit-1", "pay", wh.MTGO, "withdraw", None),
    ("return:wd:commit-1", "receive", wh.MTGO, "returned", None),
    (None, "withdraw", wh.MTGO, "withdraw", None),
    ("admin", "receive", wh.ADJUST, "adjust", None),
    ("430aa70e-cf55-4f0e-86ef-36e7174518fb", "pay", wh.TRANSFER, "pay", None),
])
def test_classify_maps_every_source_the_codebase_writes(source, kind, category, event, ref):
    origin = wh.classify(_tx(kind=kind, source=source))
    assert (origin.category, origin.event, origin.ref) == (category, event, ref)


def test_refund_carries_its_reason():
    """The reason is in the key, so a cancelled draft's refund can still explain
    itself after the draft row it points at has been deleted."""
    assert wh.classify(_tx(source="draft-refund:cancelled:sid1:p1:0")).detail == "cancelled"


def test_a_tournament_refund_is_not_read_as_an_entry():
    """Verify a tournament refund classifies as a refund, not an entry."""
    origin = wh.classify(_tx(source="refund:tourney:3:18", kind="receive"))
    assert origin.event == "refund"


def test_an_unknown_source_degrades_to_a_plain_transfer():
    """A writer that ships a new source shape before this table learns it must
    produce a plainer line, never an exception in front of a player's money."""
    origin = wh.classify(_tx(source="some-future-thing:9", kind="pay"))
    assert origin.category == wh.TRANSFER


async def _seed_tournament(tid=3, name="Summer Cube Cup"):
    async with db_session() as session:
        session.add(Tournament(id=tid, guild_id="g1", name=name,
                               total_rounds=3, current_round=0))
        await session.commit()


@pytest.mark.asyncio
async def test_labels_name_the_draft_and_the_tournament(test_db):  # noqa: F811
    await seed_session(session_id="sid1", friendly_id="worthy-knight-72")
    await _seed_tournament()

    labels = await wh.resolve_labels([
        wh.Origin(wh.DRAFT, "entry", "sid1"),
        wh.Origin(wh.TOURNAMENT, "entry", "3"),
    ])

    assert labels == {(wh.DRAFT, "sid1"): "worthy-knight-72",
                      (wh.TOURNAMENT, "3"): "Summer Cube Cup"}


@pytest.mark.asyncio
async def test_a_deleted_draft_simply_has_no_label(test_db):  # noqa: F811
    """Cancelling a draft deletes its row (views.py:2820) while the refund rows
    that cancellation books live on, so this is a routine path, not an edge case."""
    labels = await wh.resolve_labels([wh.Origin(wh.DRAFT, "refund", "gone", "cancelled")])
    assert labels == {}


@pytest.mark.asyncio
async def test_rows_that_name_nothing_cost_no_queries(test_db):  # noqa: F811
    labels = await wh.resolve_labels([wh.Origin(wh.MTGO, "deposit"),
                                      wh.Origin(wh.TRANSFER, "pay")])
    assert labels == {}


def _line(tx, labels=None):
    return wh.line(tx, wh.classify(tx), labels or {})


def test_a_named_draft_entry_reads_as_a_fee_for_that_draft():
    tx = _tx(kind="pay", amount=-10, source="draft-entry:sid1:p1:0:0-10")
    tx.created_at = datetime(2026, 9, 1, 12, 0)
    stamp = int(tx.created_at.timestamp())
    assert _line(tx, {(wh.DRAFT, "sid1"): "worthy-knight-72"}) == (
        f"`−10` Entry fee · worthy-knight-72 — <t:{stamp}:R>")


def test_an_unnamed_refund_still_says_why():
    tx = _tx(kind="receive", amount=10, source="draft-refund:cancelled:sid1:p1:0")
    assert "Draft refund (cancelled)" in _line(tx)


def test_an_unnamed_entry_drops_the_name_rather_than_printing_a_uuid():
    tx = _tx(kind="pay", amount=-10, source="draft-entry:sid1:p1:0:0-10")
    assert "sid1" not in _line(tx)
    assert "Entry fee" in _line(tx)


def test_a_deposit_names_the_mtgo_account_it_came_from():
    tx = _tx(kind="deposit", amount=60, source="serve", counterparty_id="hoaxed")
    assert "Deposit from MTGO (hoaxed)" in _line(tx)


def test_a_withdrawal_and_its_return_do_not_read_as_the_same_event():
    """A rejected withdraw books the tix straight back (mtgo_resolution_service
    _return_in_flight), so a player whose withdraw failed has both legs in their
    ledger. Reading them apart must not come down to spotting the sign."""
    out, back = _withdrawal_and_its_return()

    assert "Withdrawal to MTGO" in _line(out)
    assert "Withdrawal returned" in _line(back)
    assert _line(out).lstrip("`+-−0123456789 ") != _line(back).lstrip("`+-−0123456789 ")


def test_a_withdrawal_does_not_print_the_holder_it_was_committed_to():
    """A withdraw parks the tix with the in-flight holder, so its counterparty
    is `system:in-flight` -- our own bookkeeping, not something to show a
    player. Only the deposit's counterparty is a real MTGO account."""
    out, back = _withdrawal_and_its_return()

    assert _line(out) == "`−50` Withdrawal to MTGO"
    assert _line(back) == "`+50` Withdrawal returned"


def test_a_debt_settlement_mentions_the_other_person():
    tx = _tx(kind="receive", amount=20, source="debt:abc", counterparty_id="132868471192158208")
    assert "Debt settled ↔ <@132868471192158208>" in _line(tx)


def test_direction_comes_from_the_amount_not_the_kind():
    """pay and receive are symmetric legs of one transfer; only the sign says
    which side of it this holder is on."""
    sent = _tx(kind="pay", amount=-10, source="uuid-1", counterparty_id="111")
    got = _tx(kind="receive", amount=10, source="uuid-1", counterparty_id="222")
    assert "Sent to <@111>" in wh.line(sent, wh.classify(sent), {})
    assert "Received from <@222>" in wh.line(got, wh.classify(got), {})


def test_a_transfer_to_a_system_holder_mentions_nobody():
    """A synthetic holder is not a person to @-mention."""
    tx = _tx(kind="pay", amount=-10, source="uuid-2", counterparty_id="prize:tourney:3")
    assert "<@" not in _line(tx)


def test_an_adjustment_shows_the_note_that_explains_it():
    """An adjustment is booked by a shell script, so the note is the only thing
    that says what it was."""
    tx = _tx(kind="adjust", amount=7, source="admin", notes="failed deposit (by admin)")
    assert _line(tx) == "`+7` Adjustment · failed deposit (by admin)"


def test_an_adjustment_with_no_note_still_says_what_it_was():
    assert _line(_tx(kind="adjust", amount=-3, source="admin")) == "`−3` Adjustment"


def test_a_row_with_no_timestamp_still_renders():
    assert "— <t:" not in _line(_tx(kind="pay", amount=-5, source="uuid-3"))


def test_the_field_is_trimmed_to_what_discord_accepts():
    lines = [f"`−1` A very long wallet history line number {i}" for i in range(60)]
    field = wh.fit_field(lines)
    assert len(field) <= wh.FIELD_LIMIT
    assert field.endswith("…")


def test_a_lone_line_that_fits_is_not_given_an_ellipsis_it_did_not_earn():
    """A line one character short of the field is not truncated, so nothing
    should suggest it was."""
    text = "x" * (wh.FIELD_LIMIT - 1)
    assert wh.fit_field([text]) == text


def _long_named(source, name, amount=-1000):
    """A row whose name is longer than any line could hold."""
    tx = _tx(kind="pay", amount=amount, source=source)
    tx.created_at = datetime(2026, 9, 1, 12, 0)
    category = wh.classify(tx).category
    return wh.line(tx, wh.classify(tx), {(category, "3"): name})


def test_a_long_tournament_name_is_cut_rather_than_costing_a_row_its_page():
    """Tournament.name is String(128) and a note String(256), so a page of them
    would overflow the field -- and fit_field's overflow is dropped rows, which
    a fixed page size then makes unreachable rather than merely truncated."""
    name = "The Exceedingly Long Late Summer Vintage Cube Championship Invitational"
    text = _long_named("tourney:3:18", name)

    assert name not in text
    assert name[:20] in text
    assert "…" in text
    # A whole page of the worst case still fits, so fit_field never bites.
    assert wh.fit_field([text] * 10) == "\n".join([text] * 10)


def test_a_long_adjustment_note_is_cut_the_same_way():
    tx = _tx(kind="adjust", amount=7, source="admin", notes="n" * 256)
    tx.created_at = datetime(2026, 9, 1, 12, 0)
    text = _line(tx)

    assert "n" * 256 not in text and "…" in text
    assert wh.fit_field([text] * 10) == "\n".join([text] * 10)


def test_a_long_mtgo_username_is_cut_the_same_way():
    """counterparty_id is String(64) and nothing validates the MtgoAccount
    username it is copied from, so a deposit is the third field on a line with
    no length of its own."""
    username = "m" * 64
    tx = _tx(kind="deposit", amount=100, counterparty_id=username)
    tx.created_at = datetime(2026, 9, 1, 12, 0)
    text = _line(tx)

    assert username not in text and "…" in text
    assert wh.fit_field([text] * 10) == "\n".join([text] * 10)


@pytest.mark.asyncio
async def test_describe_rows_renders_a_whole_page(test_db):  # noqa: F811
    await seed_session(session_id="sid1", friendly_id="worthy-knight-72")
    rows = [_tx(kind="pay", amount=-10, source="draft-entry:sid1:p1:0:0-10"),
            _tx(kind="receive", amount=27, source="draft-payout:sid1:p1")]
    lines = await wh.describe_rows(rows)
    assert all("worthy-knight-72" in text for text in lines)
    assert "Entry fee" in lines[0] and "Draft winnings" in lines[1]


async def _seed_rows(n, guild="g1", player="p1"):
    """n credits, oldest first, so page 0 holds the newest."""
    for i in range(n):
        await ws.credit_done(guild, player, i + 1, job_id=f"job-{i}")


@pytest.mark.asyncio
async def test_a_page_holds_its_slice_and_the_full_total(test_db):  # noqa: F811
    await _seed_rows(23)
    page = await wh.get_history_page("g1", "p1", page=0, size=10)
    assert len(page.rows) == 10
    assert page.total == 23
    assert page.pages == 3


@pytest.mark.asyncio
async def test_the_last_page_is_the_remainder(test_db):  # noqa: F811
    await _seed_rows(23)
    page = await wh.get_history_page("g1", "p1", page=2, size=10)
    assert len(page.rows) == 3


@pytest.mark.asyncio
async def test_an_exact_multiple_does_not_grow_an_empty_page(test_db):  # noqa: F811
    await _seed_rows(20)
    assert (await wh.get_history_page("g1", "p1", size=10)).pages == 2


@pytest.mark.asyncio
async def test_a_page_size_of_zero_is_not_a_division_by_zero(test_db):  # noqa: F811
    """Nothing asks for one today, but a page of nothing would crash the panel
    on the page count rather than render an empty one."""
    await _seed_rows(3)
    page = await wh.get_history_page("g1", "p1", size=0)
    assert page.total == 3 and page.pages == 3


@pytest.mark.asyncio
async def test_a_page_past_the_end_clamps_to_the_last_one(test_db):  # noqa: F811
    await _seed_rows(23)
    page = await wh.get_history_page("g1", "p1", page=99, size=10)
    assert page.page == 2 and len(page.rows) == 3


@pytest.mark.asyncio
async def test_an_empty_ledger_is_one_empty_page(test_db):  # noqa: F811
    page = await wh.get_history_page("g1", "p1")
    assert page.rows == [] and page.total == 0 and page.pages == 1


@pytest.mark.asyncio
async def test_a_page_holds_only_this_holder_in_this_guild(test_db):  # noqa: F811
    await _seed_rows(3)
    await _seed_rows(2, player="p2")
    await _seed_rows(4, guild="g2")
    assert (await wh.get_history_page("g1", "p1")).total == 3
