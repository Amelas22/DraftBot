"""What the client does with what a serve actually puts on the wire.

The other client tests stub `_call` and assert on the arguments it was handed,
which is the right way to test what each method ASKS FOR -- and it leaves
`_call` itself, where every one of these decisions lives, untested. These run a
real HTTP server and a real aiohttp session against it, because the decisions
are all about things a stub cannot produce: a status code, an empty body, a
body that is not JSON, a connection that dies after the request went out.

Three of them are load-bearing for money and cards:

  * A 404 with `mark_missing` is the serve stating a fact -- it has never heard
    of this job -- where a 500 is the serve failing to answer. One means the
    trade can never report and the loan rolls back; the other means try later.
  * An empty 200 is "yes, and nothing to add", not a failure. Read as None it
    would look like the serve was unreachable.
  * A POST that dies AFTER the request went out may have landed. That is
    `{"_ambiguous": True}` -- hold, never refund -- and it must be told apart
    from a connection that was never established, which is a definite no.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.mtgo_tradebot_client import MtgoTradeBotClient

pytestmark = pytest.mark.asyncio

TOKEN = "contract-token"


@asynccontextmanager
async def a_serve(routes, timeout=20.0):
    """A real HTTP serve on a real socket, and a client pointed at it.

    `routes` maps (method, path) to a handler. Anything not listed 404s, which
    is what the serve does too.
    """
    app = web.Application()
    for (method, path), handler in routes.items():
        app.router.add_route(method, path, handler)
    server = TestServer(app)
    await server.start_server()
    client = MtgoTradeBotClient(url=str(server.make_url("")).rstrip("/"),
                                token=TOKEN, timeout=timeout)
    try:
        yield client, server
    finally:
        await client.close()
        await server.close()


def _json(payload, status=200):
    async def handler(request):
        return web.json_response(payload, status=status)
    return handler


# --- who the serve thinks is calling ----------------------------------------

async def test_every_request_carries_the_bearer_token():
    """The serve owns a real MTGO collection and is token-protected. A client
    that forgot the header would be refused, and the refusal reads as "the
    serve is down" to everything upstream."""
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"ok": True})

    async with a_serve({("GET", "/health"): handler}) as (client, _):
        assert await client.health() == {"ok": True}

    assert seen["auth"] == f"Bearer {TOKEN}"


# --- a fact, a failure, and the difference ----------------------------------

async def test_a_job_the_serve_has_never_heard_of_is_a_fact_not_a_failure():
    """404 + mark_missing is the one answer that lets a loan be rolled back.
    Everything else has to leave it alone, because a trade that might have
    happened must not be written off."""
    async with a_serve({}) as (client, _):                 # nothing registered
        assert await client.get_job("nope", mark_missing=True) == {"_missing": True}


async def test_the_same_404_without_the_flag_is_just_a_failure():
    """Opt-in, so every caller written against "None means something went
    wrong" keeps that contract."""
    async with a_serve({}) as (client, _):
        assert await client.get_job("nope") is None


async def test_a_serve_that_errors_says_nothing_at_all():
    """A 500 body is not an answer. Returning it would let an error page be
    read as a job projection."""
    async with a_serve({("GET", "/jobs/j1"): _json({"id": "j1"}, status=500)}) \
            as (client, _):
        assert await client.get_job("j1", mark_missing=True) is None


# --- bodies that are not a dict of the expected shape -----------------------

async def test_a_yes_with_nothing_to_add_is_not_a_failure():
    """Read as None, a 204-shaped answer would look exactly like an
    unreachable serve -- and the two want opposite handling everywhere."""
    async def empty(request):
        return web.Response(status=200, text="")

    async with a_serve({("GET", "/health"): empty}) as (client, _):
        assert await client.health() == {}


async def test_a_body_that_is_not_json_comes_back_rather_than_raising():
    """A proxy's error page, say. `_call`'s whole contract is that it never
    raises to the caller, and the text is what somebody debugging needs."""
    async def html(request):
        return web.Response(status=200, text="<h1>Bad Gateway</h1>",
                            content_type="text/html")

    async with a_serve({("GET", "/health"): html}) as (client, _):
        assert await client.health() == {"raw": "<h1>Bad Gateway</h1>"}


# --- the answer that was lost -----------------------------------------------

async def test_an_order_whose_answer_is_lost_is_ambiguous_not_refused():
    """The request reached the serve and a real MTGO trade may be open. Told
    apart from a refusal because the two have opposite recoveries: hold and go
    looking for the trade, versus give the deposit back.

    A whole cube has been handed out on this distinction being right.

    Dropped mid-flight rather than timed out, which is both the shape this
    actually takes in production -- a relay that stops carrying the reply once
    the request is through -- and the only shape that does not depend on
    ORDER_TIMEOUT_S being a number a test can wait out.
    """
    async def takes_it_then_vanishes(request):
        await request.read()                 # the order is in
        request.transport.close()            # ...and the answer never comes
        return web.Response()

    async with a_serve({("POST", "/deposit"): takes_it_then_vanishes}) \
            as (client, _):
        answer = await client.deposit("Someone", [{"name": "Swamp", "qty": 1}],
                                      wait_minutes=1)

    assert answer == {"_ambiguous": True}


async def test_an_order_is_given_its_own_deadline_not_the_session_s():
    """A read that cannot be answered in 20 seconds means the serve is down,
    and finding that out quickly is the point. An ORDER is different: the serve
    resolves every distinct card name against the MTGO collection before it
    answers, so a cube chunk of ~300 names legitimately takes minutes.

    On the session's read timeout every large deposit would be abandoned as
    ambiguous while the serve went on to open a real trade -- the worst of both
    answers, on the path that moves the most cards.
    """
    async def slow(request):
        await asyncio.sleep(0.6)
        return web.json_response({"id": "job-slow"}, status=202)

    async with a_serve({("POST", "/deposit"): slow,
                        ("GET", "/health"): slow}, timeout=0.2) as (client, _):
        assert await client.deposit("Someone",
                                    [{"name": "Swamp", "qty": 1}]) == {"id": "job-slow"}
        assert await client.health() is None, \
            "a read on the same session still gives up quickly"


async def test_an_order_that_never_reached_the_serve_is_a_definite_no():
    """A connection that was never established cannot have moved anything, so
    there is nothing to go looking for and the deposit can be handed back."""
    async with a_serve({}) as (client, server):
        await server.close()                 # the serve is gone before we ask
        assert await client.deposit("Someone",
                                    [{"name": "Swamp", "qty": 1}]) is None


async def test_a_read_that_is_lost_is_never_ambiguous():
    """Ambiguity is opt-in and only for requests that CREATE something. A
    polled read that times out has changed nothing, and answering it with
    `_ambiguous` would send the caller hunting a job it already has."""
    async def never_answers(request):
        await asyncio.sleep(5)
        return web.json_response({})

    async with a_serve({("GET", "/jobs/j1"): never_answers}, timeout=0.3) \
            as (client, _):
        assert await client.get_job("j1") is None


# --- what an order looks like on the wire -----------------------------------

async def test_a_deck_goes_out_as_per_card_quantities_and_no_scalar_qty():
    """Read off the request the SERVE received rather than the arguments the
    client was given. The serve rejects items[] and qty together -- they
    disagree about what qty means -- so a stray qty is a refused order, and
    the refusal happens on the far side of the stub the other tests use.
    """
    got = {}

    async def handler(request):
        got.update(await request.json())
        return web.json_response({"id": "job-1"}, status=202)

    deck = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]
    async with a_serve({("POST", "/borrow"): handler}) as (client, _):
        assert await client.borrow("Borrower", deck) == {"id": "job-1"}

    assert got["items"] == deck
    assert "qty" not in got, "items[] and qty together is a refused order"
    assert got["user"] == "Borrower"


async def test_a_202_is_an_acceptance_not_an_error():
    """The serve answers an accepted order with 202 and the job id to poll.
    Anything that treated 2xx as only 200 would read every accepted trade as a
    failure while the trade went ahead."""
    async with a_serve({("POST", "/deposit"): _json({"id": "job-9"}, status=202)}) \
            as (client, _):
        assert await client.deposit("Someone",
                                    [{"name": "Swamp", "qty": 1}]) == {"id": "job-9"}


async def test_a_disabled_client_never_opens_a_socket():
    """No token means no configuration, and a client that guessed would be
    trading out of whichever MTGO account answered."""
    reached = []

    async def handler(request):
        reached.append(request.path)
        return web.json_response({"ok": True})

    async with a_serve({("GET", "/health"): handler}) as (client, _):
        client.token = ""
        assert await client.health() is None

    assert reached == []
