"""What /borrow and /return say back.

The service decides what happened; the cog's whole job is turning that into a
sentence the player can act on. That matters more than it looks: every refusal
here is a dead end unless it names the next move. "Something went wrong" leaves
a player with a deck they cannot collect and no idea whether to wait, relink, or
ask an organiser.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import stub_library

from cogs.card_lending_commands import CardLendingCommands

pytestmark = pytest.mark.asyncio

DECK = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]
# What the library can hand over. The dispatch checks the shelf before it
# opens a trade, and these tests never write custody rows, so without this
# every one of them refuses with "short_cards".
_STOCK = {c["name"]: 99 for c in DECK}


def _ctx():
    ctx = SimpleNamespace()
    ctx.author = SimpleNamespace(id=1234)
    ctx.guild = SimpleNamespace(id=99)
    ctx.guild_id = 99
    ctx.defer = AsyncMock()
    ctx.followup = SimpleNamespace(send=AsyncMock())
    return ctx


def _said(ctx):
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


async def _run(monkeypatch, command, status, loan=None, busy=None, waited=False):
    import cogs.card_lending_commands as mod
    cog = CardLendingCommands(bot=SimpleNamespace())
    ctx = _ctx()
    # Borrows and returns both queue behind the serve and report out of band,
    # so both are driven through the same detached followup below.
    monkeypatch.setattr(mod, "borrow_when_free", AsyncMock(return_value=(status, waited)))
    monkeypatch.setattr(mod, "return_when_free", AsyncMock(return_value=(status, waited)))
    monkeypatch.setattr(mod, "library_busy_reason", AsyncMock(return_value=busy))
    monkeypatch.setattr(mod, "active_loan", AsyncMock(return_value=loan))
    # The shortfall path has its own tests; these cover what each status says,
    # so a library that cannot cover the deck would short-circuit all of them.
    monkeypatch.setattr(mod, "shortfall", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "poll_until_settled", AsyncMock(return_value=(status, None)))
    # The real spawn_followup returns a Task and is not awaited by the cog, so
    # capture the coroutine and drive it here -- otherwise the followup that
    # carries every message would never run.
    detached = []
    monkeypatch.setattr(mod, "spawn_followup", lambda label, coro: detached.append(coro))
    # The guild gate is exercised by its own tests below; these cover what each
    # service status says back, so a guild without a configured library would
    # otherwise short-circuit every one of them.
    monkeypatch.setattr(mod, "library_gate", lambda ctx: None)
    # Likewise the invite list and the library lookup, which have their own
    # tests: these drive the cog with the services mocked and should not reach
    # a database to do it.
    stub_library(monkeypatch, mod, stock=_STOCK)
    monkeypatch.setattr(mod, "may_borrow", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "custodian_name", AsyncMock(return_value="Team01"))
    await getattr(cog, command).callback(cog, ctx)
    for coro in detached:
        await coro
    return _said(ctx)


# --- borrowing --------------------------------------------------------------

async def test_a_borrower_with_no_deck_is_told_there_is_nothing_waiting(monkeypatch):
    said = await _run(monkeypatch, "borrow", "no_loan")
    assert "no deck" in said.lower()


async def test_an_unlinked_borrower_is_pointed_at_link(monkeypatch):
    """The one refusal the player can fix themselves in ten seconds."""
    said = await _run(monkeypatch, "borrow", "not_linked")
    assert "/link" in said


async def test_a_dispatched_borrow_tells_them_to_accept_the_trade(monkeypatch):
    """The bot cannot complete a trade alone -- if nobody says this, the job
    sits for ten minutes and fails."""
    said = await _run(monkeypatch, "borrow", "dispatched",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1))
    assert "mtgo" in said.lower() and "accept" in said.lower()


async def test_a_borrow_already_in_flight_does_not_read_as_an_error(monkeypatch):
    said = await _run(monkeypatch, "borrow", "already_in_flight")
    assert "already" in said.lower()


async def test_someone_holding_their_deck_is_pointed_at_return(monkeypatch):
    said = await _run(monkeypatch, "borrow", "already_borrowed")
    assert "/return" in said


async def test_an_offline_library_says_so_rather_than_failing_silently(monkeypatch):
    said = await _run(monkeypatch, "borrow", "unavailable")
    assert "unavailable" in said.lower() or "offline" in said.lower()


# --- returning --------------------------------------------------------------

async def test_returning_nothing_says_you_have_nothing_out(monkeypatch):
    said = await _run(monkeypatch, "return_cards", "not_borrowed")
    assert "don't have" in said.lower() or "nothing" in said.lower()


async def test_a_dispatched_return_also_tells_them_to_accept(monkeypatch):
    said = await _run(monkeypatch, "return_cards", "dispatched",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1))
    assert "mtgo" in said.lower() and "accept" in said.lower()


# --- every status is answerable --------------------------------------------

@pytest.mark.parametrize("status", [
    "no_loan", "not_linked", "dispatched", "already_in_flight",
    "already_borrowed", "not_borrowed", "unavailable", "dispatch_failed",
    "dispatch_unknown", "short_funds", "still_busy",
])
async def test_no_status_leaves_the_player_without_an_answer(monkeypatch, status):
    """A status the cog forgot would otherwise defer and never reply, which
    Discord shows as a failed interaction."""
    said = await _run(monkeypatch, "borrow", status, loan=SimpleNamespace(cards=DECK, state="assigned", id=1))
    assert said.strip(), f"status {status!r} produced no message"


# --- gating and whose trade it is ------------------------------------------

async def test_a_guild_without_a_library_is_told_so(monkeypatch):
    """The feature is per-guild; a server that never enabled it should not get
    a trade prompt for an account it has nothing to do with."""
    import cogs.card_lending_commands as mod
    monkeypatch.setattr(mod, "library_gate", lambda ctx: "The card library isn't set up here.")
    cog = CardLendingCommands(bot=SimpleNamespace())
    ctx = _ctx()
    await cog.borrow.callback(cog, ctx)
    assert "isn't set up" in _said(ctx)


async def test_the_gate_does_not_price_anything(monkeypatch):
    """It used to, and that was the bug: a guild-wide collateral was read here,
    so a server whose cubes are all FREE was told to enable the wallet before
    anybody could borrow. What a deck costs belongs to the cube it came from,
    and this runs before any cube is known -- it guards /mydeck and /return
    too, which have no cube until a loan is looked up.

    The wallet requirement moved to the charge itself, which is the only place
    that knows whether this particular cube costs anything. Asserting on the
    source because the stale read is invisible otherwise: the guild config
    still holds a collateral_tix, it simply must not decide anything now.
    """
    import inspect
    import cogs.card_lending_commands as mod

    source = inspect.getsource(mod.library_gate)
    assert "card_library_collateral" not in source
    assert "is_money_server" not in source


async def test_a_free_library_needs_no_money_server(monkeypatch):
    """The case that was broken in Cube Night: every cube free, no wallet, and
    the gate refused everyone."""
    import cogs.card_lending_commands as mod
    monkeypatch.setattr(mod, "is_money_server", lambda gid: False)
    assert mod.library_gate(_ctx()) is None


async def test_the_borrower_is_told_which_bot_is_trading_with_them(monkeypatch):
    """Two bot accounts now offer trades -- the wallet's and the library's. A
    prompt that does not name one leaves the player guessing which window is
    theirs."""
    import cogs.card_lending_commands as mod
    monkeypatch.setattr(mod, "custodian_name", AsyncMock(return_value="Team01"))
    said = await _run(monkeypatch, "borrow", "dispatched",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1))
    assert "Team01" in said


# --- waiting for the library ------------------------------------------------

async def test_a_busy_library_tells_the_borrower_to_stay_put(monkeypatch):
    """Not 'try again later': the serve frees up on its own, and asking a player
    to poll a bot is how you get five /borrow calls and five queued trades."""
    said = await _run(monkeypatch, "borrow", "dispatched", busy="busy with 1 trade",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1), waited=True)
    assert "someone else" in said.lower() or "trading with" in said.lower()
    assert "next" in said.lower()


async def test_the_borrower_is_told_when_their_turn_actually_comes(monkeypatch):
    """The whole point of holding their place -- the second message is what
    makes the first one honest."""
    said = await _run(monkeypatch, "borrow", "dispatched", busy="busy with 1 trade",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1), waited=True)
    assert "your turn" in said.lower()
    assert "accept it" in said.lower()


async def test_a_free_library_does_not_announce_a_queue(monkeypatch):
    said = await _run(monkeypatch, "borrow", "dispatched",
                      loan=SimpleNamespace(cards=DECK, state="assigned", id=1), waited=False)
    assert "your turn" not in said.lower(), "nobody queued, so nothing to announce"
    assert "accept it" in said.lower()


async def test_giving_up_on_a_long_queue_says_nothing_was_charged(monkeypatch):
    said = await _run(monkeypatch, "borrow", "still_busy", busy="busy with 1 trade")
    assert "nothing has been charged" in said.lower()


async def test_a_short_wallet_is_told_the_deposit_and_the_gap(monkeypatch):
    """Straight through the command, not just the string: the figures have to
    be fetched and rendered on the path the player actually walks."""
    import cogs.card_lending_commands as mod
    monkeypatch.setattr(mod, "deposit_shortfall",
                        AsyncMock(return_value={"deposit": 5, "fee": 0, "need": 5,
                                                "have": 2, "short": 3}))

    said = await _run(monkeypatch, "borrow", "short_funds")

    assert "5" in said and "2" in said and "3" in said


async def test_both_library_commands_give_the_shared_trade_instructions(monkeypatch):
    """The serve's protocol is one protocol: it messages the player, they reply
    YES, and it opens the trade. /borrow and /return drive the same serve as
    /wallet deposit and withdraw, so they must not describe it differently --
    telling a player to "accept the trade" leaves them waiting at a message
    that needs an answer first. Asserted on what is SENT, so a command that
    imports the shared prompt and forgets to render it still fails.
    """
    from helpers.money_gate import mtgo_trade_prompt
    shared = mtgo_trade_prompt("Team01")
    loan = SimpleNamespace(cards=DECK, state="assigned", id=1, job_id="J1")

    borrowed = await _run(monkeypatch, "borrow", "dispatched", loan=loan)
    returned = await _run(monkeypatch, "return_cards", "dispatched", loan=loan)

    assert shared in borrowed, "/borrow must render the shared instructions"
    assert shared in returned, "/return must render the shared instructions"


async def test_the_trade_message_lists_what_is_actually_being_traded(monkeypatch):
    """A borrower who took a partial deck is sent a trade for the partial deck.
    Listing the whole deck beside it invites them to reject a trade that is
    missing cards they were told to expect."""
    loan = SimpleNamespace(cards=[{"name": "Swamp", "qty": 40}],
                           pending_cards=[{"name": "Swamp", "qty": 24}],
                           state="assigned", id=1, job_id="J1")

    said = await _run(monkeypatch, "borrow", "dispatched", loan=loan)

    assert "24× Swamp" in said
    assert "40× Swamp" not in said, "the untrimmed deck is not what is in the window"


async def test_a_short_wallet_still_gets_an_answer_if_the_figures_fail(monkeypatch):
    """The figures are a nicety; answering at all is not.

    Rendering them costs a wallet read, so this branch can raise where the old
    static string could not -- and a raise here leaves the interaction deferred
    and never answered, which Discord shows as "the application did not
    respond". The player is told they are short either way.
    """
    import cogs.card_lending_commands as mod

    async def unreadable(*a, **k):
        raise RuntimeError("wallet unavailable")
    monkeypatch.setattr(mod, "deposit_shortfall", unreadable)

    said = await _run(monkeypatch, "borrow", "short_funds")

    assert said.strip(), "the player must still be told something"
    assert "deposit" in said.lower()


async def test_an_unconfigured_library_says_so_instead_of_queueing(monkeypatch):
    """Without MTGO_LENDING_URL the client is disabled, `vault()` answers None
    so nothing looks short, and the busy check reads "can't reach the
    custodian" -- so the borrower was told "you're next", waited out the whole
    five-minute queue and got "try again shortly", which invites them to do it
    all over again. `_dispatch` does return "unavailable" for a disabled
    client; it was simply never reached from behind the queue.
    """
    import cogs.card_lending_commands as mod
    stub_library(monkeypatch, mod, collateral=0, stock=_STOCK)
    monkeypatch.setattr(mod, "get_lending_client",
                        lambda: SimpleNamespace(enabled=False))

    blocked = mod.library_gate(_ctx())

    assert blocked, "a library that cannot reach its serve must refuse up front"
    assert "configured" in blocked.lower() or "unavailable" in blocked.lower()


@pytest.mark.asyncio
async def test_an_uninvited_borrower_is_turned_away(monkeypatch):
    """The pilot gate, pinned at the cog rather than at the service.

    Every other test here mocks the invite check to True so it can reach the
    status it is about, which would leave nothing asserting the gate is wired
    up at all -- the failure mode where a check exists, passes its own tests,
    and is never called.
    """
    import cogs.card_lending_commands as mod

    cog = CardLendingCommands(bot=SimpleNamespace())
    ctx = _ctx()
    monkeypatch.setattr(mod, "library_gate", lambda ctx: None)
    stub_library(monkeypatch, mod, stock=_STOCK)
    monkeypatch.setattr(mod, "may_borrow", AsyncMock(return_value=False))
    # A deck already assigned out of ANOTHER library. The loan is looked up
    # before the gate runs, on purpose: a deck carries the library it came out
    # of, and gating it on whichever library the server is bound to today would
    # hand a rebound server's drafters cards the original library never
    # invited them to.
    monkeypatch.setattr(mod, "active_loan",
                        AsyncMock(return_value=SimpleNamespace(
                            id=1, library_id="other", cards=DECK)))

    await mod.CardLendingCommands.borrow.callback(cog, ctx)

    said = ctx.followup.send.await_args.args[0]
    assert "invite-only" in said.lower()
    assert mod.may_borrow.await_args.args[0] == "other", \
        "the gate must ask the loan's library, not the server's current one"

