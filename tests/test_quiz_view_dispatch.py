"""Ephemeral quiz views must dispatch per-instance, not per-class.

py-cord's ViewStore keys live views by (component_type, message_id,
custom_id) — and ephemeral views sent via interaction.response.send_message
are registered with message_id=None (the message id isn't known at send
time), so a HARDCODED custom_id collapses every live instance of that view
into a single process-wide dispatch slot: the newest registration silently
evicts the previous one. With two players (or one player pressing Play
twice) the evicted prompt's clicks misroute into the newest instance, and
once that instance stops (the #355 edit-in-place flow stops each consumed
view), the evicted prompt's buttons dispatch to nothing — Discord shows
"This interaction failed", permanently.

The fix: ephemeral views must let py-cord auto-generate a unique custom_id
per instance (omit the custom_id argument). Only PERSISTENT views
(timeout=None, re-registered on restart) need fixed ids.

These tests exercise py-cord's real ViewStore dispatch — not the button
callbacks directly, which would bypass the routing layer under test.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from discord.ui.item import Item
from discord.ui.view import View, ViewStore

from quiz_views_module.trophy_quiz_views import (
    TrophyDecideView, TrophyGuessView, TrophyShareView)

DECKS = [{"slot": "A", "wins": 3, "user_id": "u1"},
         {"slot": "B", "wins": 0, "user_id": "u2"}]


def _user(uid):
    return SimpleNamespace(id=uid)


def _ephemeral_views(uid):
    """One instance of every per-user ephemeral trophy-quiz view."""
    return [
        TrophyGuessView("qz", DECKS, _user(uid)),
        TrophyDecideView("qz", DECKS, _user(uid), [3, 0]),
        TrophyShareView(user=_user(uid), emoji_line="🟩", total_points=5,
                        quiz_id="qz", display_id=1),
    ]


@pytest.mark.asyncio
async def test_ephemeral_views_have_per_instance_component_ids():
    """No dispatchable component may share a custom_id across two fresh
    instances — a shared id means a shared (and evictable) dispatch slot.
    (async: discord View construction requires a running event loop.)"""
    for a, b in zip(_ephemeral_views(1), _ephemeral_views(2)):
        ids_a = {c.custom_id for c in a.children if c.is_dispatchable()}
        ids_b = {c.custom_id for c in b.children if c.is_dispatchable()}
        shared = ids_a & ids_b
        assert not shared, (
            f"{type(a).__name__} shares dispatch ids across instances: {shared}")


def _dispatched_to(store, custom_id, message_id, routed):
    routed.clear()
    store.dispatch(2, custom_id, SimpleNamespace(
        message=SimpleNamespace(id=message_id), data={}))
    return routed[0] if routed else None


@pytest.mark.asyncio
async def test_concurrent_guess_prompts_dispatch_independently():
    """Two live guess prompts (two players, or one player double-clicking
    Play): each Submit routes to its own view, and one view stopping must
    not kill the other's buttons."""
    routed = []
    with patch.object(View, "_dispatch_item",
                      lambda self, item, i: routed.append(self)), \
         patch.object(Item, "refresh_state", lambda self, i: None):
        store = ViewStore(state=SimpleNamespace())
        view_a = TrophyGuessView("qz", DECKS, _user(1))
        view_b = TrophyGuessView("qz", DECKS, _user(2))
        # Register exactly as InteractionResponse.send_message does for
        # ephemerals: with no message id.
        store.add_view(view_a, message_id=None)
        store.add_view(view_b, message_id=None)

        def submit_id(v):
            return next(c.custom_id for c in v.children
                        if getattr(c, "label", "") == "Submit")

        # A clicks Submit on A's message; the payload carries the custom_id
        # baked into A's message components.
        assert _dispatched_to(store, submit_id(view_a), 111, routed) is view_a
        assert _dispatched_to(store, submit_id(view_b), 222, routed) is view_b

        # A's flow advances and its consumed view stops (#355 behavior) —
        # B's prompt must remain fully dispatchable.
        view_a.stop()
        assert _dispatched_to(store, submit_id(view_b), 222, routed) is view_b
