"""The six library workflows, driven end to end against a mock serve.

Every other test of this feature stops at a seam. The cog tests mock the
services out; the service tests call them directly and never render a message;
the settlement tests hand-write the job projection. Each is right about its own
layer and none of them can catch a break BETWEEN two layers -- which is where
every bug this feature has actually shipped has been: a deposit booked from the
order rather than the trade, a withdrawal that asked for a name MTGO does not
use, a chunked order adopting its own earlier trade.

So these drive the real commands, through the real services, against a real
database, and assert on the LEDGER -- what the library owes whom -- rather than
on what was said. The only stand-in is the serve.

The serve stand-in holds a real vault. There is one MTGO account behind every
library, and cards physically leaving it is what stops them being lent twice;
a fake whose stock never moves cannot tell a working library from one that
hands out the same Swamp to four people.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import cogs.library_commands as cog_mod
import services.card_deposit_service as deposit_svc
import services.card_lending_service as lending_svc
import services.card_library_inventory as inventory
from conftest import FakeLendingServe, a_library
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.mtgo_account import MtgoAccount

pytestmark = pytest.mark.asyncio

GUILD, CUBE, LIB = "g-e2e", "e2ecube", "lib-e2e"
ALICE, BOB = "disc-alice", "disc-bob"
ALICE_MTGO, BOB_MTGO = "AliceMTGO", "BobMTGO"


class OneMtgoAccount(FakeLendingServe):
    """The shared serve, with a vault that actually moves.

    Two things the base fake leaves still, because its own tests do not need
    them and a multi-step arc cannot do without either:

    A fresh job per trade. The base fake answers every dispatch with the same
    id, which is fine for one trade and wrong for a workflow -- the second
    dispatch would find the first one's row already filed and settle against
    it.

    A vault that gains what is deposited and loses what is lent. Custody is
    bookkeeping, but `lendable_now` is the MINIMUM of the ledger and the
    physical stock, so a vault that never empties cannot show the difference
    between the two -- which is the whole reason the minimum is taken.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._n = 0
        self._instead: "tuple[str, str] | None" = None

    def _fresh_job(self):
        self._n += 1
        self.job_id = f"job-{self._n}"
        # Accepted the moment it is offered. A trade left open is its own
        # scenario and has its own tests; these are about the happy path
        # reaching the ledger intact.
        self.jobs[self.job_id] = {"state": "done"}
        return self.job_id

    def _move(self, cards, sign):
        for c in cards or []:
            name, qty = c["name"], int(c["qty"])
            left = self.stock.get(name, 0) + sign * qty
            if left > 0:
                self.stock[name] = left
            else:
                self.stock.pop(name, None)

    async def deposit(self, user, cards, qty=1, **kw):
        self._fresh_job()
        crossed = self._swapped(cards)
        self._move(crossed, +1)
        answer = await super().deposit(user, cards, qty, **kw)
        if crossed is not cards:
            # What the trade CARRIED, beside the order it answered -- which is
            # the pair settlement has to tell apart.
            asked, instead = self._instead
            # Both sides stated outright, the way the real projection carries
            # them: `receive` is the order, `receivedActual` is what crossed.
            # Leaving the outcome to be inferred is exactly the bug -- it comes
            # back equal to the ask, which is what settlement used to book.
            self.jobs[self.job_id] = {
                "id": self.job_id, "state": "done",
                "receive": cards, "receivedActual": crossed,
                "substitutions": [{"direction": "received", "got": instead,
                                   "satisfies": asked}]}
        return answer

    def _swapped(self, cards):
        if not self._instead:
            return cards
        asked, instead = self._instead
        if not any(c["name"] == asked for c in cards or []):
            return cards
        return [{**c, "name": instead} if c["name"] == asked else c for c in cards]

    async def borrow(self, user, cards, qty=1, **kw):
        self._fresh_job()
        self._move(cards, -1)
        return await super().borrow(user, cards, qty, **kw)

    async def withdraw_cards(self, user, cards=None, qty=None, **kw):
        self._fresh_job()
        self._move(self.returns if self.returns is not None else cards, -1)
        return await super().withdraw_cards(user, cards, qty, **kw)

    async def return_cards(self, user, card=None, qty=None, **kw):
        self._fresh_job()
        self._move([c for _, cards in self.lent for c in cards], +1)
        return await super().return_cards(user, card, qty, **kw)

    def hands_over(self, asked: str, instead: str) -> None:
        """Make every later trade carry `instead` wherever it was asked `asked`.

        What MTGO does with a Universes Beyond card: the cube lists the Marvel
        name, the client trades the in-universe one, and the serve reports
        both -- the order in `receive`, what crossed in `receivedActual`, and
        the pairing in `substitutions`. The two names are one card with one
        oracle id, so this is a rename and not a short fill.
        """
        self._instead = (asked, instead)


# --- the harness ------------------------------------------------------------

CUBE_CARDS = [{"name": "Lightning Bolt", "qty": 2},
              {"name": "Counterspell", "qty": 1},
              {"name": "Island", "qty": 1}]


def _ctx(who=ALICE):
    """A context that records what the player was told."""
    return SimpleNamespace(
        author=SimpleNamespace(id=who),
        guild=SimpleNamespace(id=GUILD), guild_id=GUILD,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()))


def _said(ctx):
    return "\n".join(str(c.args[0]) for c in ctx.followup.send.await_args_list
                     if c.args)


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    """One library, one serve, two linked drafters, and the cube on offer."""
    serve = OneMtgoAccount()
    # Every module that reaches for the serve, because the arcs cross all
    # three: the cog asks who the custodian is, and the two services dispatch.
    for mod in (cog_mod, deposit_svc, lending_svc):
        monkeypatch.setattr(mod, "get_lending_client", lambda: serve)
    monkeypatch.setattr(inventory, "get_lending_client", lambda: serve, raising=False)
    # Detached followups are the only way most of these commands say anything,
    # so they are collected and driven rather than dropped on the floor.
    pending: list = []
    monkeypatch.setattr(cog_mod, "spawn_followup",
                        lambda label, coro: pending.append(coro))

    async def _fetch(cube_id):
        return CUBE_CARDS if cube_id == CUBE else None
    monkeypatch.setattr(inventory, "fetch_cube", _fetch)
    # The name-translation table is left REAL and starts empty, which is both
    # the honest starting state and how a library learns: an empty table
    # renames nothing, and the substitution arc below fills it by depositing.

    await a_library(LIB, guild=GUILD, collateral=0, cubes=(CUBE,))
    await MtgoAccount.link(ALICE, ALICE_MTGO)
    await MtgoAccount.link(BOB, BOB_MTGO)
    return SimpleNamespace(serve=serve, pending=pending)


async def _drain(library):
    """Run every followup the commands detached, including any they spawn."""
    while library.pending:
        await library.pending.pop(0)


# `return` is a python keyword, so the one command whose method name cannot
# match what a player types. Mapped here so these read as the commands they
# are rather than as the methods behind them.
_METHOD = {"return": "return_cards"}


async def _run(library, command, *args, who=ALICE):
    cog = cog_mod.LibraryCommands(bot=SimpleNamespace())
    ctx = _ctx(who)
    await getattr(cog, _METHOD.get(command, command)).callback(cog, ctx, *args)
    await _drain(library)
    return ctx


async def _owed_to(player):
    """What the library owes this player back -- custody, in the ledger."""
    import services.debt_service as debt_service
    import services.wallet_service as wallet_service
    rows = await debt_service.get_open_card_positions(
        wallet_service.library_scope(LIB), player, wallet_service.HOUSE_LIBRARY)
    return {r["card_name"]: r["net"] for r in rows if r["net"]}


# --- arc one: cards in, cards back out --------------------------------------

async def test_a_cube_deposited_is_held_listed_and_handed_back(library):
    """The custody round trip, which is the whole of what a sponsor does.

    Asserted on the ledger at each step rather than on the messages: a command
    that says "deposited" and books nothing is the failure mode, and it reads
    identically to success from the reply alone.
    """
    await _run(library, "deposit", CUBE)

    assert await _owed_to(ALICE) == {"Lightning Bolt": 2, "Counterspell": 1,
                                     "Island": 1}
    assert library.serve.stock == {"Lightning Bolt": 2, "Counterspell": 1,
                                   "Island": 1}, "and they physically arrived"

    listed = await _run(library, "deposits")
    assert "Lightning Bolt" in _said(listed)

    await _run(library, "withdraw")

    assert await _owed_to(ALICE) == {}, "the library owes them nothing now"
    assert library.serve.stock == {}, "and the shelf is empty again"


async def test_a_second_deposit_of_the_same_cube_asks_for_nothing(library):
    """Topping up is the default, so the second run has nothing to send --
    and must not quietly take a second copy of a cube somebody happens to own
    twice."""
    await _run(library, "deposit", CUBE)
    before = len(library.serve.deposited)

    said = _said(await _run(library, "deposit", CUBE))

    assert len(library.serve.deposited) == before, "nothing was offered"
    assert "already has enough" in said, said


async def test_what_one_person_deposits_is_not_owed_to_another(library):
    """One shelf, one MTGO account, and separation that is bookkeeping only --
    so the ledger is the only thing standing between a depositor and somebody
    else's cards."""
    await _run(library, "deposit", CUBE)

    assert await _owed_to(BOB) == {}
    said = _said(await _run(library, "withdraw", who=BOB))
    assert "isn't holding any of your cards" in said, said
    assert await _owed_to(ALICE), "and hers are untouched"


# --- arc two: a drafted deck out and back -----------------------------------

SESSION = "sess-e2e"


async def _a_finished_draft(pools):
    """A draft of this cube whose log gives each drafter the named cards."""
    carddata, users = {}, {}
    for seat, (who, names) in enumerate(pools.items()):
        ids = []
        for name in names:
            cid = f"c{len(carddata)}"
            carddata[cid] = {"name": name}
            ids.append(cid)
        users[f"dm{seat}"] = {"userName": who, "seatNum": seat, "cards": ids}
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id=SESSION, guild_id=GUILD, cube=CUBE,
                           draft_data={"carddata": carddata, "users": users},
                           sign_ups={ALICE: "Alice", BOB: "Bob"}))
        await s.commit()


async def test_a_drafter_collects_their_pool_and_gives_it_back(library):
    """The arc the feature exists for: somebody drafts a cube they do not own,
    the library hands them the cards, and they hand them back.

    Every step is a real MTGO trade against a vault that moves, so the one
    thing this cannot pass by accident is the one that matters -- cards leaving
    the shelf when they are collected, and being back on it afterwards.
    """
    await _run(library, "deposit", CUBE, who=BOB)          # Bob stocks the shelf
    stocked = dict(library.serve.stock)

    await _a_finished_draft({"Alice": ["Lightning Bolt", "Island"],
                             "Bob": ["Counterspell"]})
    from services.draft_deck_assignment import assign_drafted_decks
    assert await assign_drafted_decks(SESSION) == 2, "both drafters got a deck"

    waiting = await lending_svc.active_loan(ALICE)
    assert waiting.state == "assigned"

    said = _said(await _run(library, "deck"))
    assert "run `/library borrow`" in said, said

    await _run(library, "borrow")

    assert (await lending_svc.active_loan(ALICE)).state == "borrowed"
    assert library.serve.stock["Lightning Bolt"] == stocked["Lightning Bolt"] - 1, \
        "the card she collected physically left the shelf"

    await _run(library, "return")

    assert await lending_svc.active_loan(ALICE) is None, "the loan is closed"
    assert library.serve.stock["Lightning Bolt"] == stocked["Lightning Bolt"], \
        "and it is back on the shelf for the next drafter"


async def test_a_deck_out_on_loan_cannot_also_be_withdrawn(library):
    """The library owes Bob his cards and cannot hand them over, because Alice
    is holding them. Named rather than traded for: the trade would open, find
    the binder short, and fail in front of him."""
    await _run(library, "deposit", CUBE, who=BOB)
    await _a_finished_draft({"Alice": ["Lightning Bolt"], "Bob": []})
    from services.draft_deck_assignment import assign_drafted_decks
    await assign_drafted_decks(SESSION)
    await _run(library, "borrow")

    said = _said(await _run(library, "withdraw", who=BOB))

    assert "out on loan" in said, said
    assert "Lightning Bolt" in said, "and it says which"
    assert await _owed_to(BOB), "the ledger still says they are his"


# --- arc three: the card MTGO calls something else --------------------------

async def test_a_substituted_card_is_held_and_returned_under_the_name_that_moved(
        library):
    """The failure this whole arc exists for, reproduced end to end.

    A cube lists a Universes Beyond card by its Marvel name; MTGO trades the
    in-universe printing of the same oracle id. Custody used to be booked from
    the ORDER, so the library recorded a name MTGO has never heard of -- and
    the withdrawal that followed asked for that name and was answered
    `409 asked for 1x ... but only 0 held`, with the cards sitting on the shelf
    the whole time under their real name.

    Three separate things have to hold for the round trip to survive it, and
    only an arc can check that they hold TOGETHER: the deposit books what
    crossed, the pairing is learned, and the withdrawal asks in MTGO's
    vocabulary.
    """
    library.serve.hands_over("Lightning Bolt", "Chain Lightning")

    await _run(library, "deposit", CUBE)

    assert await _owed_to(ALICE) == {"Chain Lightning": 2, "Counterspell": 1,
                                     "Island": 1}, \
        "custody is booked under the name that actually moved"
    assert "Lightning Bolt" not in library.serve.stock

    from services.card_substitution_service import mtgo_names_for
    assert await mtgo_names_for(["Lightning Bolt"]) == \
        {"Lightning Bolt": "Chain Lightning"}, "and the pairing was learned"

    await _run(library, "withdraw")

    assert await _owed_to(ALICE) == {}, "and it all came back"
    assert library.serve.stock == {}


async def test_a_learned_substitution_reaches_the_next_cube_read(library):
    """Learning it is only half the fix. The next time this cube is read, the
    name has to be translated BEFORE the order goes out -- otherwise every
    later deposit asks for a name the serve refuses, and the serve refuses the
    whole order over one name it does not know."""
    library.serve.hands_over("Lightning Bolt", "Chain Lightning")
    await _run(library, "deposit", CUBE)
    await _run(library, "withdraw")

    seen = await inventory.cube_as_the_library_sees_it(CUBE)

    assert {c["name"] for c in seen.cards} == {"Chain Lightning", "Counterspell",
                                               "Island"}, \
        "the cube is read in the vocabulary the library trades in"
