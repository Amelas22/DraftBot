"""What a draft channel is told while its players gather in Draftmancer.

Shared by both routes -- DraftSetupManager, which hears it over its socket, and
the hosted route, which reads it from the status poll -- so the room sees the
same status message and the same ready-check wording whichever one runs it.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, TypedDict

import discord
from loguru import logger

READY_CHECK_INSTRUCTIONS = (
    "If the seating order is wrong, or if someone missed the ready check, please run `/ready` again — this will reset the seating order and start a new ready check. "
    "You can also use `/mutiny` to take control if needed."
)

READY_CHECK_TIMEOUT_SECONDS = 90
READY_CHECK_PASSED = "🎉 All drafters ready! Draft starting in 5 seconds..."
START_DELAY_SECONDS = 5


def ready_check_text(ready: int, total: int, *, first: bool = False) -> str:
    """The ready-check count; `first` is the message as it is first posted."""
    bells = "🔔🔔 " if first else ""
    return (
        f"{bells}Seating order set. Draftmancer Readycheck in progress: {ready}/{total} ready.\n"
        f"{READY_CHECK_INSTRUCTIONS}"
    )


def ready_check_deadline_text(timeout_epoch: int) -> str:
    return f"Readycheck will timeout <t:{timeout_epoch}:R>"


def ready_check_timed_out_text(seconds: int, missing: list[str]) -> str:
    missing_text = f"\nWaiting for: **{', '.join(missing)}**" if missing else ""
    return (
        f"⚠️ **Ready check failed!** Timed out after {seconds} seconds.{missing_text}\n"
        f"A new ready check will start automatically when all players are present."
        f"{READY_CHECK_INSTRUCTIONS}"
    )


def ready_check_left_text(user_name: str) -> str:
    return (
        f"⚠️ **Ready check failed!** User {user_name} joined or left during the ready check.\n"
        f"A new ready check will start automatically when all players are present."
    )


class SessionStatus(TypedDict):
    present_users: list[str]
    missing_users: list[str]
    unexpected_users: list[str]
    updated_at: str


def session_status(present: Iterable[str], missing: Iterable[str],
                   unexpected: Iterable[str] = ()) -> SessionStatus:
    return {
        'present_users': sorted(present),
        'missing_users': sorted(missing),
        'unexpected_users': sorted(unexpected),
        'updated_at': datetime.now().strftime('%H:%M:%S'),
    }


def format_status_message(status: Mapping[str, Any]) -> str:
    """The "Draft Session Status" message: who is in Draftmancer, who is awaited."""
    message_parts = [
        f"**Draft Session Status** (Updated: {status['updated_at']})"
    ]

    if status['present_users']:
        message_parts.append("\n**:black_joker: Drafters in Draftmancer:**")
        message_parts.append("\n".join(f"✅ {name}" for name in status['present_users']))

    if status['missing_users']:
        message_parts.append("\n**⏳ Waiting on:**")
        message_parts.append("\n".join(f"❌ {name}" for name in status['missing_users']))

    if status['unexpected_users']:
        message_parts.append("\n**⚠️ Unexpected Users:**")
        message_parts.append("\n".join(f"❓ {name}" for name in status['unexpected_users']))

    message_parts.append("\n**Instructions:**")

    if status['missing_users']:
        message_parts.append("• Waiting for all drafters to join Draftmancer.")
        message_parts.append("• When all drafters are present, seating order will be set automatically and a ready check will trigger.")
    else:
        message_parts.append("• All drafters are present! Setting seating order...")
        message_parts.append("• Ready check will begin shortly.")

    message_parts.append("• Ready check will time out after 60 seconds.")
    message_parts.append("• If everyone marks ready, the draft will begin.")
    message_parts.append("• Use `/ready` to start a new ready check if needed.")
    message_parts.append("• Use `/mutiny` to take control of the session if required.")

    return "\n".join(message_parts)


def mention_names(sign_ups: Mapping[str, str], names: Iterable[str]) -> list[str]:
    """Each name as a Discord mention where that is unambiguous, else plain text.

    sign_ups maps Discord id to display name, so the lookup runs backwards, and
    two players can share a display name. A wrong ping is worse than none.
    """
    counts = Counter(sign_ups.values())
    ids = {name: discord_id for discord_id, name in sign_ups.items() if counts[name] == 1}
    return [f"<@{ids[name]}>" if name in ids else name for name in names]


async def update_or_send_message(channel: Any, message_id: str | None, content: str | None = None,
                                 embed: discord.Embed | None = None,
                                 view: discord.ui.View | None = None) -> Any:
    """Edit `message_id` in `channel`, or post a new message when it is gone.

    Returns the message, or None when nothing could be edited or posted.
    """
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=content, embed=embed, view=view)
            return message
        except discord.NotFound:
            logger.debug(f"Message {message_id} not found, will send new")
        except Exception as e:
            logger.warning(f"Could not update message {message_id}: {e}")

    try:
        return await channel.send(content=content, embed=embed, view=view)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return None
