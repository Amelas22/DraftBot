import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from quiz_views_module.trophy_quiz_views import (
    build_reveal_lines,
    TrophyQuizView,
    TrophyGuessView,
    TrophyShareView,
)
from services.trophy_quiz_service import score_submission


_DECKS = [
    {"slot": "A", "drafter_id": "u1", "wins": 3, "pool": "1 Bolt", "mpt_url": "https://magicprotools.com/deck/show?id=A"},
    {"slot": "B", "drafter_id": "u2", "wins": 0, "pool": "1 Elf", "mpt_url": "https://magicprotools.com/deck/show?id=B"},
]


@pytest.mark.asyncio
async def test_public_view_has_no_selects_only_a_play_button():
    """The public message must NOT carry record dropdowns — shared selects on one
    message collide across concurrent users. Only a Play button (+ View Decklists
    + link buttons) belongs on the public view."""
    view = TrophyQuizView("q1", _DECKS)
    assert not any(isinstance(c, discord.ui.Select) for c in view.children)
    play = [c for c in view.children if getattr(c, "custom_id", None) == "trophy_quiz_play"]
    assert len(play) == 1


@pytest.mark.asyncio
async def test_guess_view_has_two_record_selects_and_submit():
    """Each player gets their OWN ephemeral guess view with the two record
    dropdowns + Submit (isolated per user)."""
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    selects = [c for c in gv.children if isinstance(c, discord.ui.Select)]
    assert len(selects) == 2
    labels = [getattr(c, "label", None) for c in gv.children]
    assert "Submit" in labels
    # ephemeral, non-persistent view
    assert gv.timeout is not None


@pytest.mark.asyncio
async def test_guess_view_has_reveal_submit_lock():
    """Reveal and Submit must be serialized on a given view instance: a
    concurrent Reveal-then-Submit (or double-click Submit) can otherwise
    interleave and dodge the reveal penalty or hit a composite-PK
    IntegrityError. The race itself isn't deterministically unit-testable;
    this guards the lock's presence alongside the existing reveal/submit
    tests."""
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    assert isinstance(gv._lock, asyncio.Lock)


def test_reveal_marks_trophy_direction_and_points():
    decks = [{"slot": "A", "drafter_id": "u1", "wins": 3},
             {"slot": "B", "drafter_id": "u2", "wins": 0}]
    guesses = [3, 0]
    # direction ok (+4), both exact (+3 each, flat) -> total 10 (constant max, see
    # services/trophy_quiz_service.py DIRECTION_POINTS/EXACT_POINTS).
    result = score_submission(guesses, [3, 0])
    lines = build_reveal_lines(decks, guesses, result)
    text = "\n".join(lines)
    assert "Deck A" in text and "3-0" in text and "🏆" in text and "<@u1>" in text
    assert "Deck B" in text and "0-3" in text
    assert "Deck A" in lines[-1]        # better-deck summary names Deck A
    assert "10" in text                 # total shown


@pytest.mark.asyncio
async def test_view_has_two_deck_link_buttons():
    decks = [
        {"slot": "A", "drafter_id": "u1", "wins": 3, "pool": "1 Bolt", "mpt_url": "https://magicprotools.com/deck/show?id=A"},
        {"slot": "B", "drafter_id": "u2", "wins": 0, "pool": "1 Elf", "mpt_url": "https://magicprotools.com/deck/show?id=B"},
    ]
    view = TrophyQuizView("q1", decks)
    link_urls = [c.url for c in view.children if getattr(c, "url", None)]
    assert "https://magicprotools.com/deck/show?id=A" in link_urls
    assert "https://magicprotools.com/deck/show?id=B" in link_urls


@pytest.mark.asyncio
async def test_share_posts_standalone_not_as_reply_to_reveal():
    """The public share must be a standalone channel message, NOT an interaction
    followup — a followup on a component interaction is posted as a reply to the
    button's message (the ephemeral reveal), whose preview leaks the drafter
    names / actual records to everyone. Regression guard for that leak."""
    user = SimpleNamespace(id=42, display_name="Alice", name="alice")
    view = TrophyShareView(user=user, emoji_line="🟩⬛", total_points=7)

    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=user,
        channel=channel,
        response=SimpleNamespace(edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.share_button.callback(interaction)

    # Public post goes to the channel directly (no reply reference to the reveal).
    channel.send.assert_awaited_once()
    interaction.followup.send.assert_not_called()
    posted = channel.send.await_args.args[0] if channel.send.await_args.args else channel.send.await_args.kwargs.get("content")
    # Leak-safe: score + emoji only, never drafter names or records.
    assert "7" in posted and "🟩⬛" in posted


@pytest.mark.asyncio
async def test_guess_view_has_reveal_button_enabled_by_default():
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    assert gv.revealed is False
    reveal = next(c for c in gv.children if getattr(c, "custom_id", None) == "trophy_quiz_reveal_names")
    assert reveal.disabled is False


@pytest.mark.asyncio
async def test_guess_view_reveal_button_predisabled_when_already_revealed():
    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user, revealed=True)
    assert gv.revealed is True
    reveal = next(c for c in gv.children if getattr(c, "custom_id", None) == "trophy_quiz_reveal_names")
    assert reveal.disabled is True


@pytest.mark.asyncio
async def test_reveal_button_records_and_shows_names(monkeypatch):
    from quiz_views_module import trophy_quiz_views as tv
    recorded = []
    async def fake_record(qid, pid): recorded.append((qid, pid))
    monkeypatch.setattr(tv, "record_reveal", fake_record)

    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    reveal = next(c for c in gv.children if getattr(c, "custom_id", None) == "trophy_quiz_reveal_names")
    interaction = SimpleNamespace(
        user=user,
        response=SimpleNamespace(edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await reveal.callback(interaction)
    assert gv.revealed is True and reveal.disabled is True
    assert recorded == [("q1", "1")]                     # persisted on click
    sent = interaction.followup.send.await_args
    text = (sent.args[0] if sent.args else sent.kwargs.get("content")) or ""
    assert "<@u1>" in text and "<@u2>" in text


@pytest.mark.asyncio
async def test_reveal_after_submit_does_not_record(monkeypatch):
    """Clicking Reveal on the same view after submitting must NOT record a reveal
    (that would desync the displayed total from the already-stored score)."""
    from quiz_views_module import trophy_quiz_views as tv
    recorded = []
    async def fake_record(qid, pid): recorded.append((qid, pid))
    monkeypatch.setattr(tv, "record_reveal", fake_record)

    user = SimpleNamespace(id=1, display_name="A", name="a")
    gv = TrophyGuessView("q1", _DECKS, user)
    gv.submitted = True                                  # already submitted on this view
    reveal = next(c for c in gv.children if getattr(c, "custom_id", None) == "trophy_quiz_reveal_names")
    interaction = SimpleNamespace(
        user=user,
        response=SimpleNamespace(edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await reveal.callback(interaction)
    assert recorded == []                                # no charge post-submit
    sent = interaction.followup.send.await_args
    text = (sent.args[0] if sent.args else sent.kwargs.get("content")) or ""
    assert "<@u1>" in text and "<@u2>" in text           # names still shown (free, already visible)
    assert "will apply" not in text                      # no penalty note
