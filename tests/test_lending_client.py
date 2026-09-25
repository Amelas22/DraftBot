"""The lending library talks to its own MTGO account, not the wallet's.

Team01 holds the cards players borrow; Sealed01 holds the tix. They are separate
MTGO accounts behind separate serves with separate bearer tokens, so one set of
credentials cannot address both -- a single client pointed at the wallet would
hand out tix and a single client pointed at the library would break the wallet.
"""
import pytest

from services.mtgo_tradebot_client import get_client, get_lending_client


@pytest.fixture(autouse=True)
def _clear_singletons():
    """Both accessors memoise, so a test that sets env must start from cold."""
    import services.mtgo_tradebot_client as m
    m._client = None
    m._lending_client = None
    yield
    m._client = None
    m._lending_client = None


def test_the_library_client_uses_the_lending_credentials(monkeypatch):
    monkeypatch.setenv("MTGO_LENDING_URL", "http://team01:8787")
    monkeypatch.setenv("MTGO_LENDING_TOKEN", "team01-token")

    client = get_lending_client()

    assert client.url == "http://team01:8787"
    assert client.token == "team01-token"
    assert client.enabled


def test_the_library_and_the_wallet_are_different_clients(monkeypatch):
    """The bug this guards: lending cards out of the wallet's account."""
    monkeypatch.setenv("MTGO_TRADEBOT_URL", "http://sealed01:8787")
    monkeypatch.setenv("MTGO_TRADEBOT_TOKEN", "sealed01-token")
    monkeypatch.setenv("MTGO_LENDING_URL", "http://team01:8787")
    monkeypatch.setenv("MTGO_LENDING_TOKEN", "team01-token")

    assert get_client().url != get_lending_client().url
    assert get_client().token != get_lending_client().token


def test_the_library_stays_disabled_without_its_own_config(monkeypatch):
    """Never silently fall back to the wallet's account: a misconfigured library
    must refuse to trade rather than give away tix from Sealed01."""
    monkeypatch.setenv("MTGO_TRADEBOT_URL", "http://sealed01:8787")
    monkeypatch.setenv("MTGO_TRADEBOT_TOKEN", "sealed01-token")
    monkeypatch.delenv("MTGO_LENDING_URL", raising=False)
    monkeypatch.delenv("MTGO_LENDING_TOKEN", raising=False)

    client = get_lending_client()

    assert not client.enabled
    assert client.url == ""


# --- a deck moves as one loan ----------------------------------------------
#
# /borrow and /return are the serve's loan endpoints: the direction is the
# endpoint, and items[] carries a quantity per card. That last part is what
# makes a deck expressible at all -- the older /request and /deposit take a card
# list with ONE scalar qty applied to every card, so 7 Swamp + 1 Ghostly Wings
# would have to be split across jobs, and a deck that half-arrives leaves a
# borrower holding some of their cards and a loan row agreeing with neither.

@pytest.mark.asyncio
async def test_lending_a_deck_posts_it_to_borrow(monkeypatch):
    client = get_lending_client()
    sent = {}

    async def capture(method, path, *, json=None, **kw):
        sent.update(method=method, path=path, json=json)
        return {"id": "job1"}

    monkeypatch.setattr(client, "_call", capture)

    deck = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]
    await client.borrow("Borrower", deck)

    assert (sent["method"], sent["path"]) == ("POST", "/borrow")
    assert sent["json"]["items"] == deck, "per-card quantities must survive"
    assert sent["json"]["user"] == "Borrower"


@pytest.mark.asyncio
async def test_returning_a_whole_loan_names_no_cards(monkeypatch):
    client = get_lending_client()
    sent = {}

    async def capture(method, path, *, json=None, **kw):
        sent.update(method=method, path=path, json=json)
        return {"id": "job1"}

    monkeypatch.setattr(client, "_call", capture)

    await client.return_cards("Borrower")

    assert (sent["method"], sent["path"]) == ("POST", "/return")
    assert "items" not in sent["json"] and "cards" not in sent["json"], \
        "a whole-loan return names nothing: the serve pins the printings it lent"
    assert sent["json"]["user"] == "Borrower"


@pytest.mark.asyncio
async def test_a_loan_that_moves_nothing_is_refused_before_the_network(monkeypatch):
    """An empty deck is a bug in the caller, not an MTGO failure -- catching it
    here keeps it out of the loan's job history."""
    client = get_lending_client()
    called = False

    async def capture(*a, **k):
        nonlocal called
        called = True
        return {"id": "job1"}

    monkeypatch.setattr(client, "_call", capture)

    assert await client.borrow("Borrower", []) is None
    assert await client.deposit("Borrower", []) is None
    assert not called, "an empty order must not reach the serve"


@pytest.mark.asyncio
async def test_a_loan_trade_is_given_a_deadline(monkeypatch):
    """waitMinutes 0 means NO LIMIT to the serve, not "use the default".

    Every real lending job so far was recorded with waitSec 2147483647 and
    waitLabel 'until ready' -- they never expire. The library trades with one
    person at a time, so an offer nobody accepts holds that slot forever and
    blocks every other borrower, and the failure path that returns a deck to
    the shelf never runs.
    """
    from services.mtgo_tradebot_client import DEFAULT_WAIT_MINUTES

    client = get_lending_client()
    sent = {}

    async def capture(method, path, *, json=None, **kw):
        sent.update(json=json)
        return {"id": "job1"}

    monkeypatch.setattr(client, "_call", capture)

    await client.borrow("Borrower", [{"name": "Swamp", "qty": 1}],
                        wait_minutes=DEFAULT_WAIT_MINUTES)

    assert sent["json"]["waitMinutes"] == DEFAULT_WAIT_MINUTES
    assert sent["json"]["waitMinutes"] > 0, "0 means wait forever"


def _status(monkeypatch, client, code, body=None):
    """A serve that answers `code` for GET /jobs/<id>."""
    import aiohttp

    class _Resp:
        status = code
        async def text(self): return "" if body is None else "{}"
        async def json(self): return body or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Session:
        def request(self, *a, **kw): return _Resp()

    monkeypatch.setattr(client, "_get_session", lambda: _Session())


@pytest.mark.asyncio
async def test_a_job_the_serve_never_heard_of_says_so(monkeypatch):
    """404 is the serve stating a fact -- it does not have this job -- where a
    500 or a dropped read is the serve failing to answer. Collapsing both into
    None is what made "has the library forgotten this?" a guess, and guessing
    it wrong rolls back a trade the borrower is looking at."""
    client = get_lending_client()
    _status(monkeypatch, client, 404)

    assert await client.get_job("gone", mark_missing=True) == {"_missing": True}


@pytest.mark.asyncio
async def test_a_serve_error_is_not_a_missing_job(monkeypatch):
    client = get_lending_client()
    _status(monkeypatch, client, 500)

    assert await client.get_job("x", mark_missing=True) is None


@pytest.mark.asyncio
async def test_the_wallet_still_sees_none_for_a_missing_job(monkeypatch):
    """mark_missing is opt-in so the wallet's poller, which treats any non-None
    result as a job it can read a state off, keeps the contract it was written
    against."""
    client = get_lending_client()
    _status(monkeypatch, client, 404)

    assert await client.get_job("gone") is None


def _listing(monkeypatch, client, jobs):
    async def call(method, path, **kw):
        assert path == "/jobs"
        return {"jobs": jobs}
    monkeypatch.setattr(client, "_call", call)


def _job(job_type="borrow", user="Borrower01", give=None, receive=None,
         state="running", id="j1"):
    from datetime import datetime, timezone
    return {"id": id, "type": job_type, "user": user, "state": state,
            "give": give or [], "receive": receive or [],
            "createdAt": datetime.now(timezone.utc).isoformat()}


DECK = [{"name": "Swamp", "qty": 8}, {"name": "Confront the Unknown", "qty": 1}]


@pytest.mark.asyncio
async def test_a_borrow_whose_response_was_lost_can_be_found_again(monkeypatch):
    """A POST that reached the serve created a real trade even though we never
    saw the 202. Adopting it is what stops the deposit being stranded and the
    borrower being told to ask an admin."""
    client = get_lending_client()
    _listing(monkeypatch, client, [_job(give=DECK)])

    found = await client.find_recent_deck_job("borrow", "Borrower01", DECK)

    assert found is not None and found["id"] == "j1"


@pytest.mark.asyncio
async def test_a_return_is_matched_on_what_the_bot_receives(monkeypatch):
    """The deck is on the other side of the job for a return -- matching `give`
    for both would silently never adopt one."""
    client = get_lending_client()
    _listing(monkeypatch, client, [_job(job_type="return", receive=DECK)])

    assert await client.find_recent_deck_job("return", "Borrower01", DECK) is not None


@pytest.mark.asyncio
async def test_a_different_deck_is_not_adopted(monkeypatch):
    """Adopting the wrong job attaches the loan to a trade moving other cards,
    and settlement would then record those as what the borrower owes."""
    client = get_lending_client()
    _listing(monkeypatch, client, [_job(give=[{"name": "Swamp", "qty": 4}])])

    assert await client.find_recent_deck_job("borrow", "Borrower01", DECK) is None


@pytest.mark.asyncio
async def test_another_players_job_is_not_adopted(monkeypatch):
    client = get_lending_client()
    _listing(monkeypatch, client, [_job(user="SomeoneElse", give=DECK)])

    assert await client.find_recent_deck_job("borrow", "Borrower01", DECK) is None


@pytest.mark.asyncio
async def test_a_failed_job_is_not_adopted(monkeypatch):
    """A trade that already failed moved nothing; adopting it would settle the
    loan as a failure that this attempt never actually made."""
    client = get_lending_client()
    _listing(monkeypatch, client, [_job(give=DECK, state="failed")])

    assert await client.find_recent_deck_job("borrow", "Borrower01", DECK) is None


@pytest.mark.asyncio
async def test_the_tix_scan_still_works_for_the_wallet(monkeypatch):
    """find_recent_job keeps its signature: the wallet path calls it with a
    scalar quantity and must not have to learn a new one."""
    from services.mtgo_tradebot_client import EVENT_TICKET
    client = get_lending_client()
    _listing(monkeypatch, client,
             [_job(job_type="deposit", receive=[{"name": EVENT_TICKET, "qty": 5}])])

    assert await client.find_recent_job("deposit", "Borrower01", 5) is not None
