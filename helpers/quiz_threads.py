"""Per-quiz discussion (spoiler) threads: spawn one on a quiz message, and route
the Share Results button into it. Best-effort — a thread is a nice-to-have, never
a hard dependency, and share routing always falls back to the channel."""

from typing import Optional

import discord
from loguru import logger

THREAD_ARCHIVE_MINUTES = 4320  # 3 days


async def spawn_discussion_thread(message, name: str, starter: str) -> Optional[discord.Thread]:
    """Create a public discussion thread on `message` and post the starter text.
    Returns the thread, or None if creation fails (e.g. missing create_public_threads)."""
    try:
        thread = await message.create_thread(name=name, auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        await thread.send(starter)
        return thread
    except Exception as e:
        logger.warning(f"[quiz-thread] could not create discussion thread: {e}")
        return None


async def post_quiz_share(interaction, quiz_message_id, text: str) -> None:
    """Post the share text into the quiz's discussion thread (its id == the quiz
    message id), or fall back to the interaction's channel. Never raises — a share
    is best-effort, so a resolve/send failure degrades rather than surfacing as a
    failed interaction."""
    thread = None
    if quiz_message_id:
        try:
            tid = int(quiz_message_id)
            thread = interaction.guild.get_thread(tid) if interaction.guild else None
            if thread is None:
                thread = interaction.client.get_channel(tid)
        except Exception:
            thread = None

    # Try the thread first; if its send fails (e.g. archived+locked), fall back to
    # the channel. Swallow a final failure so Share never errors the interaction.
    if thread is not None:
        try:
            await thread.send(text)
            return
        except Exception as e:
            logger.warning(f"[quiz-thread] share to thread failed, falling back to channel: {e}")
    try:
        await interaction.channel.send(text)
    except Exception as e:
        logger.warning(f"[quiz-thread] share to channel failed: {e}")
