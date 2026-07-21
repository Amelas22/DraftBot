import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models import TrophyQuizSession, TrophyQuizSubmission
from quiz_views_module.trophy_quiz_views import (
    build_reveal_lines,
    TrophyQuizView,
    TrophyGuessView,
    TrophyDecideView,
    TrophyShareView,
)
from services.trophy_quiz_service import score_submission


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db'); tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose(); os.unlink(tmp.name)


_DECKS = [
    {"slot": "A", "drafter_id": "u1", "wins": 3, "pool": "1 Bolt", "mpt_url": "https://magicprotools.com/deck/show?id=A"},
    {"slot": "B", "drafter_id": "u2", "wins": 0, "pool": "1 Elf", "mpt_url": "https://magicprotools.com/deck/show?id=B"},
]


class _FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        pass


def _interaction(user):
    return SimpleNamespace(
        user=user,
        response=_FakeResponse(),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()),
        guild=None,
        client=SimpleNamespace(get_channel=lambda tid: None),
    )


async def _seed_session(quiz_id="q1"):
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(TrophyQuizSession(
                quiz_id=quiz_id, display_id=1, guild_id="g", channel_id="c",
                message_id="555", draft_session_id="d",
                decks=_DECKS, posted_by="mod", total_participants=0,
            ))


async def _submission(quiz_id, user_id):
    async with AsyncSessionLocal() as s:
        return await s.get(TrophyQuizSubmission, (quiz_id, user_id))


# ---- static view structure -------------------------------------------------

@pytest.mark.asyncio
async def test_public_view_has_no_selects_only_a_play_button():
    view = TrophyQuizView("q1", _DECKS)
    assert not any(isinstance(c, discord.ui.Select) for c in view.children)
    play = [c for c in view.children if getattr(c, "custom_id", None) == "trophy_quiz_play"]
    assert len(play) == 1


@pytest.mark.asyncio
async def test_guess_view_has_two_record_selects_and_submit():
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    selects = [c for c in gv.children if isinstance(c, discord.ui.Select)]
    assert len(selects) == 2
    labels = [getattr(c, "label", None) for c in gv.children]
    assert "Submit" in labels
    assert gv.timeout is not None
    assert isinstance(gv._lock, asyncio.Lock)  # serializes a user's own Submit clicks


@pytest.mark.asyncio
async def test_change_mode_prefills_dropdowns_with_initial_guess():
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user, initial_guesses=[2, 1])
    assert gv.selections == {"A": 2, "B": 1}
    # the matching option is marked default on each select
    for select_ in [c for c in gv.children if isinstance(c, discord.ui.Select)]:
        want = 2 if select_.slot == "A" else 1
        defaulted = [int(o.value) for o in select_.options if o.default]
        assert defaulted == [want]


def test_reveal_marks_trophy_direction_and_points():
    decks = [{"slot": "A", "drafter_id": "u1", "wins": 3},
             {"slot": "B", "drafter_id": "u2", "wins": 0}]
    guesses = [3, 0]
    result = score_submission(guesses, [3, 0])
    lines = build_reveal_lines(decks, guesses, result)
    text = "\n".join(lines)
    assert "Deck A" in text and "3-0" in text and "🏆" in text and "<@u1>" in text
    assert "Deck B" in text and "0-3" in text
    assert "Deck A" in lines[-1]
    assert "10" in text


@pytest.mark.asyncio
async def test_view_has_two_deck_link_buttons():
    view = TrophyQuizView("q1", _DECKS)
    link_urls = [c.url for c in view.children if getattr(c, "url", None)]
    assert "https://magicprotools.com/deck/show?id=A" in link_urls
    assert "https://magicprotools.com/deck/show?id=B" in link_urls


# ---- two-phase flow --------------------------------------------------------

@pytest.mark.asyncio
async def test_initial_submit_persists_pending_and_reveals_names_only(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 3, "B": 0}
    interaction = _interaction(user)
    await gv.submit_button.callback(interaction)

    sub = await _submission("q1", "1")
    assert sub is not None and sub.finalized is False        # pending
    assert sub.guesses == [3, 0] and sub.points_earned == 0
    # names revealed, but NOT records/score
    content = interaction.response.sent[-1][0]
    assert "<@u1>" in content and "<@u2>" in content
    assert "went" not in content and "pts" not in content
    # a Decide view is offered
    assert isinstance(interaction.response.sent[-1][1].get("view"), TrophyDecideView)
    # participants not incremented yet
    async with AsyncSessionLocal() as s:
        qs = await s.get(TrophyQuizSession, "q1")
    assert qs.total_participants == 0


@pytest.mark.asyncio
async def test_keep_finalizes_initial_guess_without_cost(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 3, "B": 0}          # a correct guess → base 10
    await gv.submit_button.callback(_interaction(user))

    decide = TrophyDecideView("q1", _DECKS, user, [3, 0])
    await decide.keep_button.callback(_interaction(user))

    sub = await _submission("q1", "1")
    assert sub.finalized is True and sub.changed_answer is False
    assert sub.points_earned == 10           # no −2
    async with AsyncSessionLocal() as s:
        qs = await s.get(TrophyQuizSession, "q1")
    assert qs.total_participants == 1


@pytest.mark.asyncio
async def test_change_to_different_guess_charges_two(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 0, "B": 3}          # wrong initial
    await gv.submit_button.callback(_interaction(user))

    # change mode: revise to the correct answer
    change_view = TrophyGuessView("q1", _DECKS, user, initial_guesses=[0, 3])
    change_view.selections = {"A": 3, "B": 0}
    await change_view.submit_button.callback(_interaction(user))

    sub = await _submission("q1", "1")
    assert sub.finalized is True and sub.changed_answer is True
    assert sub.guesses == [3, 0]
    assert sub.points_earned == 8            # 10 − 2


@pytest.mark.asyncio
async def test_change_mode_same_guess_is_free(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 3, "B": 0}
    await gv.submit_button.callback(_interaction(user))

    # enters change mode but submits the SAME guess → no charge
    change_view = TrophyGuessView("q1", _DECKS, user, initial_guesses=[3, 0])
    change_view.selections = {"A": 3, "B": 0}
    await change_view.submit_button.callback(_interaction(user))

    sub = await _submission("q1", "1")
    assert sub.finalized is True and sub.changed_answer is False
    assert sub.points_earned == 10           # no −2 for a no-op change


@pytest.mark.asyncio
async def test_replay_while_pending_resumes_not_fresh_guess(test_db):
    """The re-open loophole: after seeing names, re-Play must resume the
    Keep/Change choice on the LOCKED initial guess, never a fresh guess."""
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 1, "B": 2}
    await gv.submit_button.callback(_interaction(user))

    # re-Play while pending
    public = TrophyQuizView("q1", _DECKS)
    interaction = _interaction(user)
    await public.play_button.callback(interaction)
    content, kwargs = interaction.response.sent[-1]
    assert "<@u1>" in content                       # names re-shown
    view = kwargs.get("view")
    assert isinstance(view, TrophyDecideView)        # decide, not a fresh guess view
    assert view.initial_guesses == [1, 2]            # locked to the original


@pytest.mark.asyncio
async def test_replay_after_finalized_shows_result(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 3, "B": 0}
    await gv.submit_button.callback(_interaction(user))
    await TrophyDecideView("q1", _DECKS, user, [3, 0]).keep_button.callback(_interaction(user))

    public = TrophyQuizView("q1", _DECKS)
    interaction = _interaction(user)
    await public.play_button.callback(interaction)
    content, kwargs = interaction.response.sent[-1]
    assert "already submitted" in content
    assert isinstance(kwargs.get("view"), TrophyShareView)   # full result + share


@pytest.mark.asyncio
async def test_keep_is_idempotent_participants_counted_once(test_db):
    await _seed_session()
    user = SimpleNamespace(id=1, display_name="Alice", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.selections = {"A": 3, "B": 0}
    await gv.submit_button.callback(_interaction(user))
    decide = TrophyDecideView("q1", _DECKS, user, [3, 0])
    await decide.keep_button.callback(_interaction(user))
    await decide.keep_button.callback(_interaction(user))   # second click / race

    async with AsyncSessionLocal() as s:
        qs = await s.get(TrophyQuizSession, "q1")
    assert qs.total_participants == 1                        # counted once


# ---- share (leak-safe, thread-routed) --------------------------------------

@pytest.mark.asyncio
async def test_share_posts_standalone_not_as_reply_to_reveal():
    user = SimpleNamespace(id=42, display_name="Alice", name="alice")
    view = TrophyShareView(user=user, emoji_line="🟩⬛", total_points=7)
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=user, channel=channel, guild=None,
        client=SimpleNamespace(get_channel=lambda tid: None),
        response=SimpleNamespace(edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.share_button.callback(interaction)
    channel.send.assert_awaited_once()
    interaction.followup.send.assert_not_called()
    posted = channel.send.await_args.args[0] if channel.send.await_args.args else channel.send.await_args.kwargs.get("content")
    assert "7" in posted and "🟩⬛" in posted


@pytest.mark.asyncio
async def test_trophy_share_posts_via_thread_helper(test_db, monkeypatch):
    await _seed_session()
    import quiz_views_module.trophy_quiz_views as tv
    seen = {}
    async def fake_post(interaction, message_id, text):
        seen["message_id"] = message_id; seen["text"] = text
    monkeypatch.setattr(tv, "post_quiz_share", fake_post)

    user = SimpleNamespace(id=42, display_name="Alice", name="alice")
    view = TrophyShareView(user=user, emoji_line="🟩⬛", total_points=7, quiz_id="q1", display_id=1)
    interaction = SimpleNamespace(user=user, channel=SimpleNamespace(send=AsyncMock()),
                                  response=SimpleNamespace(edit_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    await view.share_button.callback(interaction)
    assert seen.get("message_id") == "555"
    assert "7" in seen["text"] and "🟩⬛" in seen["text"]
    interaction.channel.send.assert_not_called()
