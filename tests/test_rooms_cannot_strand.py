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
