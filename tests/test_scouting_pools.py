"""An opponent's pool, posted into the scouting thread already named for them.

The threads are made when the team rooms are, one per player on the OTHER team.
Until now they held notes only, while the pools went to two other places: the
team's own thread (its own players) and, on a tournament draft, one shared
thread holding all twelve. This puts each opponent's pool where somebody
scouting that opponent is already looking.

Tournament drafts only -- those pools are already in the shared thread, so this
moves nothing into view that was not there. On an ordinary draft it would be a
disclosure, which is a league decision rather than a refactor.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.draft_log_store import post_pools_to_scouting_threads


def _draft_data():
    """Two players a side, each with a one-card pool -- enough to tell whose
    pool landed where, which is the only thing these tests care about."""
    return {
        "users": {
            "dmA1": {"userName": "Ana", "cards": ["c1"], "decklist": {"main": ["c1"]}},
            "dmA2": {"userName": "Abe", "cards": ["c2"], "decklist": {"main": ["c2"]}},
            "dmB1": {"userName": "Bo", "cards": ["c3"], "decklist": {"main": ["c3"]}},
            "dmB2": {"userName": "Bex", "cards": ["c4"], "decklist": {"main": ["c4"]}},
        },
        "carddata": {
            "c1": {"name": "Ancestral Recall", "type": "Instant"},
            "c2": {"name": "Black Lotus", "type": "Artifact"},
            "c3": {"name": "Time Walk", "type": "Sorcery"},
            "c4": {"name": "Timetwister", "type": "Sorcery"},
        },
    }


MAPPING = {"a1": "dmA1", "a2": "dmA2", "b1": "dmB1", "b2": "dmB2"}
SIGN_UPS = {"a1": "Ana", "a2": "Abe", "b1": "Bo", "b2": "Bex"}
TEAM_A, TEAM_B = ["a1", "a2"], ["b1", "b2"]


def make_thread(name, already_posted=()):
    """A scouting thread that records what was sent into it.

    `already_posted` is what a previous run left behind, as the .txt filenames
    the idempotency check reads back.
    """
    sent = []

    async def send(content=None, files=None, **_):
        sent.append(SimpleNamespace(content=content, files=list(files or [])))
        return SimpleNamespace(id=len(sent))

    async def history(**_):
        for filename in already_posted:
            yield SimpleNamespace(
                author=SimpleNamespace(id=999),
                attachments=[SimpleNamespace(filename=filename)])

    return SimpleNamespace(name=name, send=AsyncMock(side_effect=send),
                           history=history, sent=sent)


def make_channel(threads):
    async def archived_threads(**_):
        for t in []:
            yield t
    return SimpleNamespace(threads=list(threads), archived_threads=archived_threads,
                           create_thread=AsyncMock())


def _bot():
    return SimpleNamespace(user=SimpleNamespace(id=999))


def posted_names(thread):
    """The .txt filenames this thread received."""
    return [f.filename for s in thread.sent for f in s.files if f.filename.endswith(".txt")]


@pytest.mark.asyncio
async def test_each_opponents_pool_lands_in_their_own_thread():
    """The whole point: the thread named for Bo carries Bo's pool, in the RED
    channel -- the channel belonging to the team scouting Bo."""
    bo, bex = make_thread("Bo"), make_thread("Bex")
    ana, abe = make_thread("Ana"), make_thread("Abe")
    red = make_channel([bo, bex])      # red scouts the blue players
    blue = make_channel([ana, abe])    # and vice versa

    await post_pools_to_scouting_threads(
        _bot(), red, blue, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert posted_names(bo) == ["Bo.txt"]
    assert posted_names(bex) == ["Bex.txt"]
    assert posted_names(ana) == ["Ana.txt"]
    assert posted_names(abe) == ["Abe.txt"]


@pytest.mark.asyncio
async def test_a_pool_is_not_posted_to_its_owners_own_team():
    """A scouting thread is for the OTHER side. Posting Ana's pool into the red
    channel would hand her own team a thread about themselves, and -- worse --
    the blue player scouting Ana would never get it."""
    ana_in_red = make_thread("Ana")
    red = make_channel([ana_in_red])
    blue = make_channel([])

    await post_pools_to_scouting_threads(
        _bot(), red, blue, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert posted_names(ana_in_red) == [], "a player's pool went to their own team"


@pytest.mark.asyncio
async def test_a_pool_already_posted_is_not_posted_again():
    """post_team_logs is reconciler-driven and re-runs freely. Without this the
    threads fill with a fresh copy of every pool on every tick."""
    bo = make_thread("Bo", already_posted=("Bo.txt",))
    red = make_channel([bo])
    blue = make_channel([])

    await post_pools_to_scouting_threads(
        _bot(), red, blue, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert bo.sent == [], "an already-posted pool was posted a second time"


@pytest.mark.asyncio
async def test_a_player_without_a_thread_is_skipped_not_created():
    """create_thread is best-effort and Discord refuses it often enough to
    matter. Creating one here would overrule whatever made it skip them."""
    bex = make_thread("Bex")
    red = make_channel([bex])          # no thread for Bo
    blue = make_channel([])

    await post_pools_to_scouting_threads(
        _bot(), red, blue, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert posted_names(bex) == ["Bex.txt"]
    red.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_failing_thread_does_not_cost_the_others_their_pools():
    """Best-effort, like the threads themselves. This runs after the team and
    open threads have already delivered; it must never raise into that path."""
    bo = make_thread("Bo")
    bo.send = AsyncMock(side_effect=RuntimeError("Discord said no"))
    bex = make_thread("Bex")
    red = make_channel([bo, bex])
    blue = make_channel([])

    await post_pools_to_scouting_threads(
        _bot(), red, blue, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert posted_names(bex) == ["Bex.txt"], "one bad thread stopped the rest"


@pytest.mark.asyncio
async def test_a_missing_channel_is_tolerated():
    """Room creation can leave one side without a resolvable channel; the other
    side's scouting should still get its pools."""
    bo = make_thread("Bo")
    red = make_channel([bo])

    await post_pools_to_scouting_threads(
        _bot(), red, None, TEAM_A, TEAM_B, MAPPING, _draft_data(), SIGN_UPS)

    assert posted_names(bo) == ["Bo.txt"]


# ---- wiring -----------------------------------------------------------------------


def _locked_source():
    import inspect
    import textwrap

    from services.draft_log_store import _post_team_logs_locked

    return textwrap.dedent(inspect.getsource(_post_team_logs_locked))


def test_post_team_logs_actually_calls_it():
    """A function nothing calls posts no pools. This is the same shape as
    test_opponent_threads_wiring: the unit tests above prove the behaviour, and
    this proves it is reachable."""
    import ast

    tree = ast.parse(_locked_source())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "post_pools_to_scouting_threads" in called


def test_it_is_gated_on_the_tournament_and_runs_after_the_open_thread():
    """Two properties in one place, because they are the same `if`.

    The gate: these pools are only already-open on a tournament match. Outside
    one, posting an opponent's pool into a team's channel discloses something
    the draft never showed -- a league decision, not this change's to make.

    The order: the shared thread is the deliverable, and a scouting-thread
    failure must not come before it.
    """
    import ast

    tree = ast.parse(_locked_source())

    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "tournament_match_id" not in test_src:
            continue
        names = [n.func.id for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        if "post_pools_to_scouting_threads" in names:
            guarded.append(names)

    assert guarded, (
        "post_pools_to_scouting_threads is not inside the tournament_match_id "
        "guard -- it would post opponent pools on ordinary drafts")
    names = guarded[0]
    assert names.index("_post_open_pools") < names.index("post_pools_to_scouting_threads"), (
        "scouting threads must be posted after the shared open-pools thread")
