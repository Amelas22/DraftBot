"""Catch two live views claiming the same component dispatch slot.

py-cord routes a component interaction by looking up
``(component_type, message_id, custom_id)`` in one process-wide dict
(``ViewStore.add_view``). An ephemeral view — anything sent with
``interaction.response.send_message(..., ephemeral=True)`` — is registered with
``message_id=None``, because the message does not exist yet at send time. That
leaves ``custom_id`` as the only thing separating two live instances, and
``add_view`` assigns into the dict unconditionally, so the newer registration
silently evicts the older one. The evicted view's components then dispatch to the
newer instance (wrong user's state) and, once that one stops, to nothing at all:
"This interaction failed", permanently.

This has been fixed by hand three times in this repo — the trophy quiz views
(d2feec1), the settle selects, and the staked bet-cap panel — each time by
removing a hardcoded ``custom_id`` from one batch of call sites, and each time
missing the next batch. This guard exists so there is no next time: it watches the
one place every registration passes through, rather than the many places
components are built.

It deliberately checks for the COLLISION rather than for hardcoded ids. A fixed
``custom_id`` is only a problem when two live views end up holding it, and a
single-instance fixed id is perfectly correct — so testing for the collision has
no false positives on legitimate uses, and catches causes other than a hardcoded
id (two persistent views accidentally sharing one, say).

Loud where a human will see it, quiet where it would hurt: raises under
TEST_MODE so a test fails on the spot, logs an error in production so a live
draft is never taken down by the diagnostic.
"""
from functools import wraps
from typing import Any

from loguru import logger

from config import is_test_mode


class DispatchCollision(RuntimeError):
    """Two live views registered the same component dispatch key."""


def _is_benign_reregistration(incoming: Any, existing: Any) -> bool:
    """Registering the same persistent view class again is normal, not a collision.

    ``bot.add_view(PublicSettleDebtsView())`` runs inside on_ready, which fires again
    on every gateway reconnect — a fresh instance, the same fixed ids, and
    ``timeout=None`` so the previous one never reports itself finished. That is the
    mechanism working as intended: persistent views are re-registered by id precisely
    so buttons survive a restart.
    """
    return (incoming.timeout is None
            and existing.timeout is None
            and type(incoming) is type(existing))


def _report(incoming: Any, existing: Any, custom_id: str,
            message_id: int | None) -> None:
    detail = (
        f"Dispatch collision on custom_id={custom_id!r} (message_id={message_id}): "
        f"{type(incoming).__name__} would evict a live {type(existing).__name__}. "
        f"Whichever was registered first stops receiving its own clicks. If these are "
        f"ephemeral views, drop the hardcoded custom_id so py-cord assigns a unique "
        f"one per instance."
    )
    if is_test_mode():
        raise DispatchCollision(detail)
    logger.error(f"[ViewDispatch] {detail}")


def install_dispatch_collision_guard() -> None:
    """Wrap ViewStore.add_view to report a collision before it happens.

    Safe to call more than once: on_ready runs again on every gateway reconnect, and
    wrapping a wrapper would report the same collision twice.
    """
    from discord.ui.view import ViewStore

    if getattr(ViewStore.add_view, "_draftbot_guarded", False):
        return

    original = ViewStore.add_view

    @wraps(original)
    def add_view(self: Any, view: Any, message_id: int | None = None) -> None:
        # Detecting and reporting are kept apart on purpose. Only the detection is
        # wrapped in a catch-all — a diagnostic must never break a live interaction —
        # and _report is called outside it, because DispatchCollision is an Exception
        # and would otherwise be swallowed by the very net meant to contain the
        # detector's own bugs.
        collision = None
        try:
            for item in view.children:
                if not item.is_dispatchable():
                    continue
                found = self._views.get((item.type.value, message_id, item.custom_id))
                if not found:
                    continue
                existing = found[0]
                if existing is view or existing.is_finished():
                    continue  # the slot is ours already, or its holder is done with it
                if _is_benign_reregistration(view, existing):
                    continue
                collision = (view, existing, item.custom_id, message_id)
                break
        except Exception as e:
            logger.warning(f"[ViewDispatch] collision check failed: {e}")

        if collision is not None:
            _report(*collision)
        return original(self, view, message_id)

    add_view._draftbot_guarded = True   # pyrefly: ignore [missing-attribute]
    ViewStore.add_view = add_view
    logger.info("[ViewDispatch] collision guard installed")
