"""Each private team text channel gets a voice channel beside it, permissioned alike.

The pairing is the point: a team that can read its own text channel and nobody
else's should be able to talk in exactly the same room. That is enforced by
handing the voice channel the SAME overwrites mapping the text channel got, so
the two cannot drift apart -- these assert on that identity, not on a re-derived
copy of the permissions.
"""
import pytest

from conftest import make_channel_harness

VOICE_ON = {"voice_channels": True}


def _harness(monkeypatch, **kwargs):
    kwargs.setdefault("features", VOICE_ON)
    return make_channel_harness(monkeypatch, **kwargs)


@pytest.mark.asyncio
async def test_each_team_channel_gets_a_paired_voice_channel(monkeypatch):
    view, guild, _db = _harness(monkeypatch)
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert len(guild.voice_calls) == 1, "no voice channel was created for the team"
    assert guild.voice_calls[0]["name"] == "Red-Team-Voice-abc1"


@pytest.mark.asyncio
async def test_the_voice_channel_carries_its_text_channel_s_permissions(monkeypatch):
    """Identity, not equality: they must be the one mapping, so a later change to
    how team permissions are built cannot apply to text and miss voice."""
    view, guild, _db = _harness(monkeypatch)
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.voice_calls[0]["overwrites"] is guild.text_calls[0]["overwrites"]


@pytest.mark.asyncio
async def test_the_voice_channel_is_cleaned_up_with_the_rest(monkeypatch):
    """Expiry deletes everything in the STORED channel_ids and nothing else, so a
    voice channel missing from it would outlive the draft forever."""
    view, guild, db = _harness(monkeypatch)
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    stored = db.persisted.get("channel_ids")
    assert stored and len(stored) == 2, (
        f"expected the text and voice channels both persisted, got {stored}")


@pytest.mark.asyncio
async def test_the_shared_draft_chat_gets_no_voice_channel(monkeypatch):
    """Team voice is for one team talking privately; the shared chat has no team."""
    view, guild, _db = _harness(monkeypatch)
    await view.create_team_channel(guild, "Draft", [], ["a1"], ["b1"])

    assert guild.voice_calls == []


@pytest.mark.asyncio
async def test_a_guild_with_voice_disabled_gets_no_voice_channel(monkeypatch):
    """Off by default: a guild that never asked for voice must not find two extra
    channels per draft in its category."""
    view, guild, _db = _harness(monkeypatch, features={"voice_channels": False})
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.voice_calls == []


@pytest.mark.parametrize("session_type", ["random", "staked", "premade"])
@pytest.mark.asyncio
async def test_every_kind_of_team_draft_gets_voice(monkeypatch, session_type):
    """create_rooms_pairings sends random, staked and premade down the SAME branch,
    creating identical private team channels. Gating voice on one of them made the
    guild's flag a half-truth: a guild that turned it on still got nothing for its
    other drafts. Being a team channel is the real precondition."""
    view, guild, _db = _harness(monkeypatch, session_type=session_type)
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert len(guild.voice_calls) == 1, f"{session_type} draft got no voice channel"


@pytest.mark.asyncio
async def test_voice_falls_back_to_the_draft_category(monkeypatch):
    """Most guilds configure no separate voice category. Landing the channel beside
    its text channel beats landing it uncategorised at the top of the server."""
    view, guild, _db = _harness(monkeypatch)
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.voice_calls[0]["category"].name == "Draft Channels"


@pytest.mark.asyncio
async def test_a_configured_voice_category_is_used_when_present(monkeypatch):
    """And it is honoured in ANY guild -- it used to be read only in the special
    guild, so the key silently did nothing everywhere else."""
    view, guild, _db = _harness(
        monkeypatch, categories=("Draft Channels", "Draft Voice"),
        extra_config={"voice": "Draft Voice"})
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.voice_calls[0]["category"].name == "Draft Voice"


@pytest.mark.asyncio
async def test_a_failed_voice_channel_never_costs_the_text_channel(monkeypatch):
    """Voice is the convenience; the text channel is the draft. Uncaught, a voice
    failure aborts room creation AND skips the UPDATE below it -- orphaning the text
    channel that WAS created: alive in Discord, unknown to the cleanup sweep."""
    view, guild, db = _harness(monkeypatch, voice_error=RuntimeError("no voice"))
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert len(guild.text_calls) == 1
    assert len(db.persisted.get("channel_ids") or []) == 1


@pytest.mark.asyncio
async def test_a_voice_failure_below_discord_is_contained_too(monkeypatch):
    """Not just HTTPException: an aiohttp connection error is not a discord.py
    exception, and has taken out a whole run in this codebase before."""
    view, guild, db = _harness(monkeypatch, voice_error=OSError("connection reset"))
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert len(db.persisted.get("channel_ids") or []) == 1


# --- reserving capacity for the voice rooms too ------------------------------

@pytest.mark.asyncio
async def test_a_voice_draft_reserves_room_for_its_voice_channels(monkeypatch):
    """Five rooms, not three. A voice-enabled guild with no separate voice
    category puts 3 text + 2 voice in the draft category, so reserving only the
    text ones re-splits the draft one layer up -- a team's voice channel ending
    up somewhere other than its text channel."""
    from helpers.draft_rooms import CATEGORY_CHANNEL_LIMIT, rooms_needed

    assert rooms_needed(voice=True) == 5
    assert rooms_needed(voice=False) == 3

    view, guild, _db = _harness(monkeypatch)
    # Room for the three text channels but not the two voice ones.
    guild.categories[0].channels = [object()] * (CATEGORY_CHANNEL_LIMIT - 4)

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    text_at = guild.text_calls[0]["category"].name
    voice_at = guild.voice_calls[0]["category"].name
    assert text_at == voice_at, (
        f"the team's text channel went to {text_at} and its voice to {voice_at}")


@pytest.mark.asyncio
async def test_a_separate_voice_category_reserves_nothing_extra(monkeypatch):
    """Voice landing in its own category costs the draft category nothing, so a
    draft must not be pushed out of a category that fits its text channels."""
    from helpers.draft_rooms import CATEGORY_CHANNEL_LIMIT

    view, guild, _db = _harness(
        monkeypatch, categories=("Draft Channels", "Draft Voice"),
        extra_config={"voice": "Draft Voice"})
    base = guild.categories[0]
    base.channels = [object()] * (CATEGORY_CHANNEL_LIMIT - 3)

    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert guild.text_calls[0]["category"] is base, "pushed out of a category that fits"
    assert guild.voice_calls[0]["category"].name == "Draft Voice"
