"""A half-created draft must be finishable, not permanent.

Room creation makes three channels (plus voice), and create_team_channel commits
each one's id in its own session as it goes. The old completeness check read
draft_chat_channel -- set while creating the FIRST of those three -- so from the
moment anything was created, every later attempt concluded the job was done. A
failure on channel two or three left the draft with a chat it could not be played
in, for good: an e2e run produced exactly that (red=None, blue=None, one id).

Two things have to hold for that to be impossible, and neither is sufficient
alone:

  1. The "finished" marker is written AFTER the work, so an unfinished run is
     indistinguishable from one that never started.
  2. Creation is re-enterable, so the retry that follows completes the draft
     instead of building a second copy of it beside the first.
"""
import pytest

from conftest import make_channel_harness

VOICE_ON = {"voice_channels": True}


@pytest.mark.asyncio
async def test_an_existing_text_channel_is_reused_not_duplicated(monkeypatch):
    """The retry after a partial run must converge on the draft that exists."""
    view, guild, _db = make_channel_harness(
        monkeypatch, seeded=[("Red-Team-Chat-abc1", "text")])

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.text_calls == [], "a second copy of the team channel was created"
    assert len(guild.text_channels) == 1


@pytest.mark.asyncio
async def test_the_reused_channel_is_still_recorded_for_cleanup(monkeypatch):
    """Reuse is only safe if the id still reaches channel_ids: the sweep deletes
    what it has stored and nothing else, so a reused-but-unrecorded channel would
    outlive the draft forever."""
    view, guild, db = make_channel_harness(
        monkeypatch, seeded=[("Red-Team-Chat-abc1", "text")])
    reused = guild.text_channels[0]

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert db.persisted.get("channel_ids") == [reused.id]


@pytest.mark.asyncio
async def test_a_partial_run_is_finished_rather_than_duplicated(monkeypatch):
    """The actual scenario: the shared chat and one team channel exist from a run
    that died, and the retry has to produce the missing one and nothing else."""
    view, guild, db = make_channel_harness(
        monkeypatch, features=VOICE_ON,
        seeded=[("Draft-Chat-abc1", "text"), ("Red-Team-Chat-abc1", "text")])

    for team in ("Draft", "Red-Team", "Blue-Team"):
        await view.create_team_channel(guild, team, [], ["a1"], ["b1"])

    created = [c["name"] for c in guild.text_calls]
    assert created == ["Blue-Team-Chat-abc1"], (
        f"expected only the missing channel to be created, got {created}")
    # Everything the draft owns is recorded, whether this run made it or not.
    assert len(db.persisted["channel_ids"]) == 5   # 3 text + 2 voice


@pytest.mark.asyncio
async def test_a_matching_voice_channel_is_reused_too(monkeypatch):
    """Voice names keep their case in Discord while text names are lowercased, so
    a case-sensitive match would find one and miss the other."""
    view, guild, _db = make_channel_harness(
        monkeypatch, features=VOICE_ON, seeded=[("Red-Team-Voice-abc1", "voice")])

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.voice_calls == [], "a second voice channel was created"


@pytest.mark.asyncio
async def test_reuse_matches_the_name_discord_actually_stored(monkeypatch):
    """Discord lowercases text channel names, so the channel that comes back does
    not equal the name we asked for. Matching case-sensitively would miss it and
    duplicate every channel on every retry -- the exact failure this prevents."""
    view, guild, _db = make_channel_harness(
        monkeypatch, seeded=[("RED-TEAM-CHAT-ABC1", "text")])

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.text_calls == []


@pytest.mark.asyncio
async def test_an_unrelated_channel_is_not_mistaken_for_this_draft_s(monkeypatch):
    """Names carry the friendly id, so a different draft's rooms look different."""
    view, guild, _db = make_channel_harness(
        monkeypatch, strays=[("Red-Team-Chat-zzz9", "text")])

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert [c["name"] for c in guild.text_calls] == ["Red-Team-Chat-abc1"]


@pytest.mark.asyncio
async def test_an_identically_named_channel_this_draft_does_not_own_is_not_adopted(
        monkeypatch):
    """friendly_id is random and explicitly NOT unique -- get_by_friendly_id
    documents that duplicates within a guild happen. So two live drafts can want
    the same channel name, and matching on name alone would hand one team the
    other team's private channel. Only channels this session recorded count."""
    view, guild, _db = make_channel_harness(
        monkeypatch, strays=[("Red-Team-Chat-abc1", "text")])

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert [c["name"] for c in guild.text_calls] == ["Red-Team-Chat-abc1"], (
        "the other draft's channel was adopted instead of creating our own")


# --- Property 3: one run at a time -----------------------------------------
#
# The two properties above concern a retry that follows a FAILED run: they are
# about time passing between attempts. Neither says anything about two attempts
# overlapping, and property 1 is what makes that possible -- writing the marker
# after the work means a run in progress is, by construction, indistinguishable
# from one that never started.
#
# Production has two callers that can both fire within seconds: _on_end_draft
# when the draft finishes, and _handle_ownership_loss_with_pairings when a
# reconnecting manager finds links already distributed. Both entered for the
# same draft on 2026-09-15 and each built the draft in full -- two sets of
# match_results, two sets of pairing messages.

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from sqlalchemy import select  # noqa: E402

import views  # noqa: E402
from database.db_session import AsyncSessionLocal  # noqa: E402
from models.draft_session import DraftSession  # noqa: E402
from models.match import MatchResult  # noqa: E402

from conftest import seed_session  # noqa: E402

RACE_ID = "race-1"


class _Chan:
    id = 111

    async def send(self, *a, **k):
        return SimpleNamespace(id=222)


async def _seed_race_draft():
    await seed_session(session_id=RACE_ID, guild="1", stype="random",
                       stage="teams",
                       teams=(["10", "11", "12"], ["20", "21", "22"]),
                       sign_ups={str(i): f"p{i}" for i in
                                 (10, 11, 12, 20, 21, 22)})
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=RACE_ID))
        row.draft_channel_id = "999"
        row.message_id = "888"
        await s.commit()


def _stub_everything_but_the_race(monkeypatch):
    """Fake every edge except the database, whose visibility IS the subject.

    Channel creation is stubbed rather than harnessed: the tests above already
    cover it, and what this one asserts -- how many times the draft got built --
    is counted in rows and pairing posts, not channels.
    """
    import livedrafts

    async def fake_create_team_channel(self, guild, team_name, *a, **k):
        if team_name == views.SHARED_CHAT_TEAM:
            self.draft_chat_channel = _Chan.id
        return _Chan.id

    monkeypatch.setattr(views.PersistentView, "create_team_channel",
                        fake_create_team_channel)
    monkeypatch.setattr(views, "get_config",
                        lambda gid: {"categories": {}, "roles": {}, "features": {}})
    monkeypatch.setattr(views, "resolve_draft_category", AsyncMock(return_value=None))
    monkeypatch.setattr(views, "_draft_rooms_needed", lambda *a, **k: 3)
    monkeypatch.setattr(views, "generate_draft_summary_embed",
                        AsyncMock(return_value=(SimpleNamespace(), None)))
    monkeypatch.setattr(views, "safe_pin", AsyncMock())
    monkeypatch.setattr(views, "update_player_stats_for_draft", AsyncMock())
    monkeypatch.setattr(views, "update_last_draft_timestamp", AsyncMock())
    monkeypatch.setattr(livedrafts, "create_live_draft_summary", AsyncMock())

    posted = AsyncMock()
    monkeypatch.setattr(views, "post_pairings", posted)
    return posted


def _gate_the_first_run(monkeypatch):
    """Park the FIRST run inside its transaction, before it writes anything.

    A plain asyncio.gather would leave the interleaving to chance, so the test
    could pass on broken code and be flaky forever after. Parking the first run
    at a known point makes the overlap the test's own doing: run two reads the
    session while run one is provably mid-transaction and uncommitted.

    The park is BEFORE calculate_pairings rather than during channel creation on
    purpose -- a run holding SQLite's write lock while parked would make the
    second run block on the database rather than on the bug under test.
    """
    real = views.calculate_pairings
    gate = asyncio.Event()
    parked = asyncio.Event()
    contended = asyncio.Event()
    first = {"seen": False}

    async def gated(session, db_session):
        if not first["seen"]:
            first["seen"] = True
            parked.set()
            await gate.wait()
        else:
            # A second run got past the read AND the rooms_created_at check
            # while the first is parked and uncommitted -- the exact overlap
            # this test exists to create. Only reachable when nothing is
            # serialising the two, so the caller waits on it with a timeout.
            contended.set()
        return await real(session, db_session)

    monkeypatch.setattr(views, "calculate_pairings", gated)
    return gate, parked, contended


@pytest.mark.asyncio
async def test_two_overlapping_runs_build_the_draft_once(monkeypatch, test_db):
    """Two callers entering together must produce ONE draft, not two.

    Fails without a lock with 18 match_results and two pairing posts: every
    match exists twice, which is what makes update_pairings_posting's
    scalar_one_or_none raise MultipleResultsFound on every reported result.
    """
    await _seed_race_draft()
    posted = _stub_everything_but_the_race(monkeypatch)
    gate, parked, contended = _gate_the_first_run(monkeypatch)

    guild = SimpleNamespace(id=1, get_member=lambda uid: SimpleNamespace(
        id=uid, display_name=str(uid)), get_channel=lambda cid: _Chan())
    bot = SimpleNamespace(get_channel=lambda cid: None)

    first = asyncio.create_task(
        views.PersistentView.create_rooms_pairings(bot, guild, RACE_ID))
    await asyncio.wait_for(parked.wait(), timeout=5)

    second = asyncio.create_task(
        views.PersistentView.create_rooms_pairings(bot, guild, RACE_ID))
    # Wait for the second run to prove it is past its own read -- not a fixed
    # number of event-loop turns, which only LOOKS deterministic and would let
    # this pass on unserialised code whenever that read happened to land after
    # the first run committed. Timing out is the serialised outcome: the second
    # run cannot get past its read, because it is still waiting for the lock.
    try:
        await asyncio.wait_for(contended.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), timeout=15)

    async with AsyncSessionLocal() as s:
        rows = (await s.scalars(select(MatchResult).where(
            MatchResult.session_id == RACE_ID))).all()

    numbers = sorted(r.match_number for r in rows)
    assert numbers == [1, 2, 3, 4, 5, 6, 7, 8, 9], (
        f"the draft was built more than once: match numbers {numbers}")
    assert posted.await_count == 1, (
        f"pairings were posted {posted.await_count} times; every extra post is "
        f"a second set of buttons players can click")
    # Exactly one run built the draft and exactly one declined. Without this a
    # pair of swallowed exceptions -- create_rooms_pairings turns any exception
    # into False -- would leave no rows and satisfy the count assertions.
    assert sorted(outcomes, key=bool) == [False, True], (
        f"expected one run to build the draft and one to decline, got {outcomes}")
