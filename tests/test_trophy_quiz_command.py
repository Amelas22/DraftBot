import os
import random
import tempfile
from datetime import datetime
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

import cogs.trophy_quiz_commands as trophy_quiz_commands
from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models import DraftSession, MatchResult, TrophyQuizSession


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db'); tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose(); os.unlink(tmp.name)


def _draft_data(dm_ids):
    """Mirrors tests/test_trophy_quiz_selection.py's fixture builder."""
    users, carddata = {}, {}
    for i, dm in enumerate(dm_ids):
        cid = f"c{i}"
        users[dm] = {"seatNum": i, "cards": [cid], "isBot": False}
        carddata[cid] = {"name": f"Card{i}"}
    return {"users": users, "carddata": carddata}


def _fake_jpeg():
    """A minimal in-memory JPEG-like BytesIO for stubbing PileImageBuilder.build."""
    buf = BytesIO()
    buf.write(b"\xff\xd8fakejpeg")
    buf.seek(0)
    return buf


def test_embed_states_one_winning_one_losing_record():
    """The public embed must make clear one deck has a winning record and the
    other a losing record (without revealing which is which)."""
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    embed = cog.create_trophy_quiz_embed(DraftSession(session_id="d", guild_id="g"), display_id=1)
    text = embed.description.lower()
    assert "winning record" in text and "losing record" in text
    assert "2-1 or 3-0" in text and "1-2 or 0-3" in text


# 6 drafters, 3 rounds; d0 goes 3-0 (extreme), d5 goes 0-3 (extreme), rest middle.
# select_two_decks succeeds for this pod (has an extreme, has a better + worse bucket).
_WITH_EXTREME = [
    ("d0", "d1", "d0"), ("d2", "d3", "d2"), ("d4", "d5", "d4"),
    ("d0", "d2", "d0"), ("d1", "d4", "d1"), ("d3", "d5", "d3"),
    ("d0", "d3", "d0"), ("d1", "d5", "d1"), ("d2", "d4", "d2"),
]
# 4 drafters, 3 rounds, all middle records -> no extreme -> select_two_decks returns None.
_NO_EXTREME = [
    ("d0", "d2", "d0"), ("d0", "d3", "d0"), ("d1", "d0", "d1"),
    ("d1", "d3", "d1"), ("d2", "d1", "d2"), ("d3", "d2", "d3"),
]


async def _seed_draft(session_id, guild_id, n, rounds, **overrides):
    sign_ups = {f"d{i}": f"n{i}" for i in range(n)}
    draft_data = _draft_data([f"dm{i}" for i in range(n)])
    kwargs = dict(
        session_id=session_id,
        guild_id=guild_id,
        spaces_object_key=f"key-{session_id}",
        session_type="random",
        draft_start_time=datetime.now(),
        sign_ups=sign_ups,
        cube="TestCube",
    )
    kwargs.update(overrides)
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(DraftSession(**kwargs))
            for i, (a, b, w) in enumerate(rounds):
                s.add(MatchResult(session_id=session_id, match_number=i, player1_id=a, player2_id=b, winner_id=w))
    return draft_data


@pytest.mark.asyncio
async def test_select_eligible_draft_skips_ineligible_draft(test_db):
    guild_id = "g1"
    ineligible_data = await _seed_draft("d-ineligible", guild_id, 4, _NO_EXTREME)
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)

    data_by_key = {
        "key-d-ineligible": ineligible_data,
        "key-d-eligible": eligible_data,
    }

    async def fake_load(object_key):
        return data_by_key[object_key]

    with patch("cogs.trophy_quiz_commands.load_from_spaces", AsyncMock(side_effect=fake_load)):
        draft, decks, draft_data = await trophy_quiz_commands._select_eligible_draft(guild_id, rng=random.Random(0))

    assert draft is not None
    assert draft.session_id == "d-eligible"
    assert decks is not None and len(decks) == 2
    assert all(deck.get("pool") for deck in decks)
    assert draft_data == eligible_data


@pytest.mark.asyncio
async def test_select_eligible_draft_none_when_already_recorded(test_db):
    guild_id = "g1"
    await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)

    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(TrophyQuizSession(
                quiz_id="g1-1",
                display_id=1,
                guild_id=guild_id,
                channel_id="c",
                draft_session_id="d-eligible",
                decks=[
                    {"slot": "A", "drafter_id": "d0", "wins": 3, "pool": "x"},
                    {"slot": "B", "drafter_id": "d5", "wins": 0, "pool": "y"},
                ],
                posted_by="mod",
            ))

    with patch("cogs.trophy_quiz_commands.load_from_spaces", AsyncMock()) as mock_load:
        draft, decks, draft_data = await trophy_quiz_commands._select_eligible_draft(guild_id, rng=random.Random(0))

    assert draft is None
    assert decks is None
    assert draft_data is None
    mock_load.assert_not_called()


class _FakeChannel:
    """Minimal stand-in for a discord.TextChannel: records sent payloads and
    returns a message whose .pin() is a no-op AsyncMock."""

    def __init__(self, channel_id=999):
        self.id = channel_id
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = AsyncMock()
        message.id = 555
        message.pin = AsyncMock()
        return message


async def _select_eligible_with_data(guild_id, draft_data, rng_seed=0):
    """Seed-and-select helper: patches load_from_spaces to return draft_data
    for any key, then runs the real _select_eligible_draft selection."""
    with patch("cogs.trophy_quiz_commands.load_from_spaces", AsyncMock(return_value=draft_data)):
        return await trophy_quiz_commands._select_eligible_draft(guild_id, rng=random.Random(rng_seed))


@pytest.mark.asyncio
async def test_create_and_post_trophy_quiz_aborts_when_mpt_submission_fails(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)

    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)
    assert draft is not None  # sanity: selection itself succeeded

    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    with patch(
        "helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
        AsyncMock(return_value=None),
    ):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id,
            channel=channel,
            draft_session=draft,
            deck_pair=deck_pair,
            posted_by="mod",
            draft_data=draft_data,
        )

    assert message is None
    assert channel.sent == []  # never posted
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(TrophyQuizSession))
        assert result.scalars().all() == []  # never persisted


@pytest.mark.asyncio
async def test_create_and_post_trophy_quiz_sets_mpt_url_on_success(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)

    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)
    assert draft is not None  # sanity: selection itself succeeded

    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    mpt_url = "https://magicprotools.com/deck/show?id=T"
    with patch(
        "helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
        AsyncMock(return_value=mpt_url),
    ), patch(
        "cogs.trophy_quiz_commands.PileImageBuilder.build",
        AsyncMock(side_effect=lambda *a, **k: _fake_jpeg()),
    ):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id,
            channel=channel,
            draft_session=draft,
            deck_pair=deck_pair,
            posted_by="mod",
            draft_data=draft_data,
        )

    assert message is not None
    assert len(channel.sent) == 1
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(TrophyQuizSession))
        quiz = result.scalar_one()
        assert len(quiz.decks) == 2
        assert all(d["mpt_url"] == mpt_url for d in quiz.decks)
        assert all("image" not in d for d in quiz.decks)


@pytest.mark.asyncio
async def test_create_and_post_aborts_when_pile_image_fails(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)
    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)
    assert draft is not None

    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    with patch("helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
               AsyncMock(return_value="https://magicprotools.com/deck/show?id=T")), \
         patch("cogs.trophy_quiz_commands.PileImageBuilder.build",
               AsyncMock(return_value=None)):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id, channel=channel, draft_session=draft,
            deck_pair=deck_pair, posted_by="mod", draft_data=draft_data,
        )

    assert message is None
    assert channel.sent == []
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(TrophyQuizSession))).scalars().all() == []


@pytest.mark.asyncio
async def test_create_and_post_attaches_two_images_on_success(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)
    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)

    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    with patch("helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
               AsyncMock(return_value="https://magicprotools.com/deck/show?id=T")), \
         patch("cogs.trophy_quiz_commands.PileImageBuilder.build",
               AsyncMock(side_effect=lambda *a, **k: _fake_jpeg())):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id, channel=channel, draft_session=draft,
            deck_pair=deck_pair, posted_by="mod", draft_data=draft_data,
        )

    assert message is not None
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert len(sent["files"]) == 2                      # two image attachments
    assert len(sent["embeds"]) == 3                     # prompt + Deck A + Deck B


@pytest.mark.asyncio
async def test_mpt_and_image_receive_split_deck(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)
    # Built decklist with a DISTINCT sideboard card + basics, so the MTGO text is
    # provably the built deck (main + basics // sideboard), not the raw pool.
    eligible_data["carddata"]["sb"] = {"name": "Negate"}
    for uid, u in eligible_data["users"].items():
        pooled = list(u["cards"])                       # each user's single drafted card
        u["cards"] = pooled + ["sb"]
        u["decklist"] = {"main": pooled, "side": ["sb"], "lands": {"U": 3}}

    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)

    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    seen_deck_texts = []

    async def fake_submit(self, dm_id, dd, deck_text):
        seen_deck_texts.append(deck_text)
        return "https://magicprotools.com/deck/show?id=T"

    build_calls = []

    async def fake_build(self, main_ids, side_ids, carddata):
        build_calls.append((main_ids, side_ids))
        return _fake_jpeg()

    with patch("helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view", fake_submit), \
         patch("cogs.trophy_quiz_commands.PileImageBuilder.build", fake_build):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id, channel=channel, draft_session=draft,
            deck_pair=deck_pair, posted_by="mod", draft_data=draft_data,
        )

    assert message is not None
    assert len(seen_deck_texts) == 2
    for text in seen_deck_texts:
        assert "\n\n" in text                       # MTGO sideboard separated by a blank line
        main_part, side_part = text.split("\n\n", 1)
        assert "Negate" in side_part                # sideboard card lands in the sideboard section
        assert "Negate" not in main_part            # ...and not in the main deck
        assert "3 Island" in main_part              # basics (U:3) rendered into the main deck
    # image built from the split's main/side lists (side = the "sb" card)
    assert len(build_calls) == 2
    assert all(isinstance(m, list) and s == ["sb"] for m, s in build_calls)


class _FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class _FakeCtx:
    """Minimal stand-in for an ApplicationContext used by post_trophy_quiz."""

    def __init__(self, guild_id="g1", author_id=1):
        self.guild = AsyncMock(); self.guild.id = guild_id
        self.author = AsyncMock(); self.author.id = author_id
        self.channel = _FakeChannel()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


@pytest.mark.asyncio
async def test_post_trophy_quiz_surfaces_error_when_create_raises(test_db):
    """An unexpected raise inside the build path must be flagged to the mod as an
    ephemeral error, not propagate past the deferred interaction."""
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    ctx = _FakeCtx()

    with patch.object(
        trophy_quiz_commands,
        "_select_eligible_draft",
        AsyncMock(return_value=(AsyncMock(), [{"drafter_id": "u1"}], {})),
    ), patch.object(
        cog, "_create_and_post_trophy_quiz",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # Must not raise out of the command.
        await trophy_quiz_commands.TrophyQuizCommands.post_trophy_quiz.callback(cog, ctx)

    assert any(
        "No eligible draft could be turned into a trophy quiz right now" in (c or "")
        for c in ctx.followup.sent
    )


@pytest.mark.asyncio
async def test_try_next_draft_skips_failed_prep(test_db, monkeypatch):
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()

    # Two candidate drafts; first prep returns None (fail), second returns a message.
    d1 = trophy_quiz_commands.DraftSession(session_id="d1", guild_id="g1")
    d2 = trophy_quiz_commands.DraftSession(session_id="d2", guild_id="g1")
    selections = [ (d1, [{"drafter_id": "u"}], {}), (d2, [{"drafter_id": "u"}], {}) ]

    async def fake_select(guild_id, rng=random, exclude_draft_ids=None):
        exclude_draft_ids = exclude_draft_ids or set()
        for draft, dp, dd in selections:
            if draft.session_id not in exclude_draft_ids:
                return draft, dp, dd
        return None, None, None

    posted = {}
    async def fake_create(self, guild_id, channel, draft_session, deck_pair, posted_by, draft_data):
        if draft_session.session_id == "d1":
            return None                      # prep failed
        posted["id"] = draft_session.session_id
        return AsyncMock()

    monkeypatch.setattr(trophy_quiz_commands, "_select_eligible_draft", fake_select)
    monkeypatch.setattr(trophy_quiz_commands.TrophyQuizCommands,
                        "_create_and_post_trophy_quiz", fake_create)

    msg = await cog._select_prep_and_post("g1", channel, "mod")
    assert msg is not None
    assert posted["id"] == "d2"              # skipped d1, posted d2


@pytest.mark.asyncio
async def test_try_next_draft_bounded_then_gives_up(test_db, monkeypatch):
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()
    calls = {"n": 0}

    async def fake_select(guild_id, rng=random, exclude_draft_ids=None):
        calls["n"] += 1
        d = trophy_quiz_commands.DraftSession(session_id=f"d{calls['n']}", guild_id="g1")
        return d, [{"drafter_id": "u"}], {}

    async def fake_create(self, *a, **k):
        return None                          # every prep fails

    monkeypatch.setattr(trophy_quiz_commands, "_select_eligible_draft", fake_select)
    monkeypatch.setattr(trophy_quiz_commands.TrophyQuizCommands,
                        "_create_and_post_trophy_quiz", fake_create)

    msg = await cog._select_prep_and_post("g1", channel, "mod")
    assert msg is None
    assert calls["n"] == trophy_quiz_commands.MAX_POST_ATTEMPTS   # bounded


@pytest.mark.asyncio
async def test_post_spawns_discussion_thread(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)
    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()
    spawned = []
    async def fake_spawn(message, name, starter):
        spawned.append(name); return None
    with patch("helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
               AsyncMock(return_value="https://magicprotools.com/deck/show?id=T")), \
         patch("cogs.trophy_quiz_commands.PileImageBuilder.build",
               AsyncMock(side_effect=lambda *a, **k: _fake_jpeg())), \
         patch("cogs.trophy_quiz_commands.spawn_discussion_thread", fake_spawn):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id, channel=channel, draft_session=draft,
            deck_pair=deck_pair, posted_by="mod", draft_data=draft_data)
    assert message is not None
    assert spawned and "Discussion (spoilers)" in spawned[0]


@pytest.mark.asyncio
async def test_post_succeeds_even_if_thread_spawn_raises(test_db):
    guild_id = "g1"
    eligible_data = await _seed_draft("d-eligible", guild_id, 6, _WITH_EXTREME)
    draft, deck_pair, draft_data = await _select_eligible_with_data(guild_id, eligible_data)
    cog = trophy_quiz_commands.TrophyQuizCommands(bot=None)
    channel = _FakeChannel()
    async def boom(*a, **k):
        raise RuntimeError("no perms")
    with patch("helpers.magicprotools_helper.MagicProtoolsHelper.submit_deck_view",
               AsyncMock(return_value="https://magicprotools.com/deck/show?id=T")), \
         patch("cogs.trophy_quiz_commands.PileImageBuilder.build",
               AsyncMock(side_effect=lambda *a, **k: _fake_jpeg())), \
         patch("cogs.trophy_quiz_commands.spawn_discussion_thread", boom):
        message = await cog._create_and_post_trophy_quiz(
            guild_id=guild_id, channel=channel, draft_session=draft,
            deck_pair=deck_pair, posted_by="mod", draft_data=draft_data)
    assert message is not None   # quiz still posted; spawn failure must not break it
