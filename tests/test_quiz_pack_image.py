import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

import cogs.quiz_commands as quiz_commands
from cogs.quiz_commands import build_pack_image_or_abort
from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models import DraftSession, QuizSession
from services.draft_analysis import DraftAnalysis


@pytest.mark.asyncio
async def test_returns_none_flag_off_no_image_expected():
    # flag off -> (file=None, abort=False): post normally, no image
    file, abort = await build_pack_image_or_abort(
        enabled=False, draft_data={"carddata": {}}, booster_ids=[], quiz_id="q", images_config={}
    )
    assert file is None and abort is False


@pytest.mark.asyncio
async def test_aborts_when_enabled_and_composite_none():
    with patch("cogs.quiz_commands.PackCompositor.create_pack_composite",
               new=AsyncMock(return_value=None)):
        file, abort = await build_pack_image_or_abort(
            enabled=True, draft_data={"carddata": {"c": {}}}, booster_ids=["c"],
            quiz_id="q", images_config={},
        )
    assert file is None and abort is True            # do not post


@pytest.mark.asyncio
async def test_returns_file_when_enabled_and_composite_ok():
    from io import BytesIO
    with patch("cogs.quiz_commands.PackCompositor.create_pack_composite",
               new=AsyncMock(return_value=BytesIO(b"\xff\xd8jpg"))):
        file, abort = await build_pack_image_or_abort(
            enabled=True, draft_data={"carddata": {"c": {}}}, booster_ids=["c"],
            quiz_id="q", images_config={},
        )
    assert file is not None and abort is False


@pytest.mark.asyncio
async def test_aborts_when_enabled_and_composite_raises():
    # A raised exception during compositing must also hard-fail (abort=True),
    # not degrade to posting text-only.
    with patch("cogs.quiz_commands.PackCompositor.create_pack_composite",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        file, abort = await build_pack_image_or_abort(
            enabled=True, draft_data={"carddata": {"c": {}}}, booster_ids=["c"],
            quiz_id="q", images_config={},
        )
    assert file is None and abort is True


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db'); tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose(); os.unlink(tmp.name)


class _FakeChannel:
    """Minimal stand-in for a discord.TextChannel: records sent payloads."""

    def __init__(self, channel_id=999):
        self.id = channel_id
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = AsyncMock()
        message.id = 555
        message.pin = AsyncMock()
        return message


def _six_player_draft_data():
    """6-player draft, pack 0 passes left; enough picks for a 4-pick trace
    starting at seat 0."""
    users = {}
    for i in range(6):
        booster = [f"c{j}" for j in range(i, 6)]
        users[f"user{i}"] = {
            "userName": f"Player{i}",
            "seatNum": i,
            "picks": [
                {"packNum": 0, "pickNum": i, "pick": [0], "booster": booster},
            ],
        }
    carddata = {f"c{i}": {"name": f"Card{i}"} for i in range(6)}
    return {"sessionID": "TEST_SESSION", "users": users, "carddata": carddata}


@pytest.mark.asyncio
async def test_create_and_post_quiz_aborts_without_orphaning_session(test_db):
    """An enabled-but-unbuildable pack image must abort with no QuizSession
    row persisted at all (a message_id-less orphan would permanently burn
    the draft+seat combo via _select_random_draft_and_seat)."""
    draft_data = _six_player_draft_data()
    analysis = DraftAnalysis(draft_data)
    pack_trace = analysis.trace_pack(pack_num=0, length=4, starting_seat=0)
    assert pack_trace is not None and len(pack_trace.picks) == 4  # sanity

    draft_session = DraftSession(
        session_id="test_session",
        guild_id="g1",
        cube="TestCube",
        draft_start_time=datetime.now(),
        spaces_object_key="key",
    )

    cog = quiz_commands.QuizCommands(bot=None)
    channel = _FakeChannel()

    fake_config = {"features": {"quiz_pack_images": {"enabled": True}}}
    with patch("cogs.quiz_commands.get_config", return_value=fake_config), \
         patch("cogs.quiz_commands.PackCompositor.create_pack_composite",
               AsyncMock(return_value=None)):
        message = await cog._create_and_post_quiz(
            guild_id="g1",
            channel_id="c1",
            channel=channel,
            draft_session=draft_session,
            analysis=analysis,
            pack_trace=pack_trace,
            mpt_url=None,
            draft_data=draft_data,
            posted_by="mod",
            starting_seat=0,
        )

    assert message is None
    assert channel.sent == []  # never posted
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(QuizSession))
        assert result.scalars().all() == []  # never persisted
