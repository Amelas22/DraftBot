import asyncio

import discord
from loguru import logger
from sqlalchemy import update
from database.db_session import db_session
from models import TrophyQuizSession, TrophyQuizSubmission
from services.trophy_quiz_service import score_submission, record_label, REVEAL_COST, apply_reveal_cost
from services.trophy_quiz_reveal_store import has_revealed, record_reveal
from helpers.display_names import get_display_name
from helpers.quiz_threads import post_quiz_share

# Record dropdown options: (wins, label). Values are the win count as a string
# ("3".."0"), matched against services.trophy_quiz_service record semantics.
RECORD_OPTIONS = [(3, "3-0"), (2, "2-1"), (1, "1-2"), (0, "0-3")]


def build_reveal_lines(decks, guesses, result, revealed: bool = False) -> list:
    """Per-deck reveal + a better-deck/points summary line.

    decks: the 2 stored deck dicts (slot/drafter_id/wins).
    guesses: [winsA, winsB] the player submitted.
    result: the dict returned by services.trophy_quiz_service.score_submission.
    revealed: when True, appends a pay-to-reveal-names penalty line and the
        penalized final score.
    """
    lines = []
    for deck, guess in zip(decks, guesses):
        trophy = " 🏆" if deck["wins"] == 3 else ""
        lines.append(
            f"**Deck {deck['slot']}** — <@{deck['drafter_id']}> went "
            f"**{record_label(deck['wins'])}**{trophy} "
            f"(you guessed {record_label(guess)})"
        )
    better = max(decks, key=lambda d: d["wins"])
    lines.append(
        f"Better deck: **Deck {better['slot']}** — "
        f"direction {'✅' if result['direction_correct'] else '❌'} "
        f"(+{result['direction_points']}), exact {sum(result['exact_points'])} → "
        f"**{result['total']} pts**"
    )
    if revealed:
        final = apply_reveal_cost(result["total"], revealed)
        lines.append(f"🔎 Revealed player names (−{REVEAL_COST}) → **{final} pts**")
    return lines


def _build_pilots_line(decks: list) -> str:
    """The "🔎 Piloted by — Deck A: @x, Deck B: @y" line shared by the Play
    button's re-show-on-revealed path and the reveal button's own reply."""
    names = ", ".join(f"Deck {d['slot']}: <@{d['drafter_id']}>" for d in decks)
    return f"🔎 Piloted by — {names}"


def _build_emoji_line(result) -> str:
    """Leak-safe per-deck emoji: 🟩 only when BOTH the overall direction call and
    that deck's exact record were correct, else ⬛. Never reveals which deck or
    what the actual records were — just correct/incorrect."""
    return "".join(
        "🟩" if result["direction_correct"] and pts > 0 else "⬛"
        for pts in result["exact_points"]
    )


class TrophyRecordSelect(discord.ui.Select):
    """Record dropdown for one deck slot ('A' or 'B').

    Lives on the per-user ephemeral TrophyGuessView, so it has no shared
    state and Discord auto-generates its custom_id (ephemeral views don't
    persist)."""

    def __init__(self, slot: str, parent_view, row: int = None):
        self.slot = slot
        self.parent_view = parent_view

        options = [
            discord.SelectOption(label=label, value=str(wins))
            for wins, label in RECORD_OPTIONS
        ]

        super().__init__(
            placeholder=f"Deck {slot}: pick a record",
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        """Store the selection (keyed by slot; the view is private to one user)
        and acknowledge the interaction."""
        if self.values:
            self.parent_view.selections[self.slot] = int(self.values[0])
            logger.debug(
                f"User {interaction.user.id} selected {self.values[0]} for Deck {self.slot} "
                f"(trophy quiz {self.parent_view.quiz_id})"
            )
        await interaction.response.defer()


class TrophyShareView(discord.ui.View):
    """View with a button to share trophy quiz results publicly.

    Leak-safe: the public share posts ONLY the player's total + a 🟩/⬛ per-deck
    emoji line and the quiz link — never drafter names or actual records. Since
    the max score is a constant 10 regardless of the actual records, the total
    itself reveals nothing either.
    """

    def __init__(self, user: discord.User, emoji_line: str, total_points: int, quiz_id: str = None, display_id: int = None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.user = user
        self.emoji_line = emoji_line
        self.total_points = total_points
        self.quiz_id = quiz_id
        self.display_id = display_id

    async def _format_quiz_reference(self) -> str:
        if not self.display_id:
            return "the Trophy Quiz"
        if not self.quiz_id:
            return f"Trophy Quiz #{self.display_id}"
        async with db_session() as session:
            quiz_session = await session.get(TrophyQuizSession, self.quiz_id)
        if quiz_session and quiz_session.message_id:
            message_link = (
                f"https://discord.com/channels/{quiz_session.guild_id}/"
                f"{quiz_session.channel_id}/{quiz_session.message_id}"
            )
            return f"[Trophy Quiz #{self.display_id}]({message_link})"
        return f"Trophy Quiz #{self.display_id}"

    @discord.ui.button(
        label="📤 Share Results Publicly",
        style=discord.ButtonStyle.primary,
        custom_id="share_trophy_quiz_results",
    )
    async def share_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Post results publicly to the channel — score + emoji line only."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You can only share your own results!",
                ephemeral=True,
            )
            return

        quiz_ref = await self._format_quiz_reference()
        quiz_num = f" #{self.display_id}" if self.display_id else ""
        share_text = f"🏆 Trophy Quiz{quiz_num}\n{self.emoji_line} {self.total_points} pts"
        display_name = get_display_name(self.user)

        message = (
            f"**{display_name}** scored **{self.total_points} points** on {quiz_ref}!\n"
            f"\n```\n{share_text}\n```"
        )

        message_id = None
        if self.quiz_id:
            async with db_session() as session:
                qs = await session.get(TrophyQuizSession, self.quiz_id)
            message_id = qs.message_id if qs else None

        button.disabled = True
        button.label = "✓ Shared!"

        await interaction.response.edit_message(view=self)
        # Post as a standalone message, NOT interaction.followup.send: a followup
        # on a component interaction is delivered as a reply to the button's
        # message (the ephemeral reveal), and Discord surfaces that reveal's
        # drafter names / actual records in the reply preview to everyone.
        # post_quiz_share does a plain .send with no reply reference (routing
        # into the quiz's discussion thread when available, else the channel),
        # so nothing leaks.
        await post_quiz_share(interaction, message_id, message)

        logger.info(
            f"User {self.user.id} ({get_display_name(self.user)}) shared trophy quiz "
            f"results publicly: {self.total_points} points"
        )


async def _load_display_id(quiz_id: str):
    async with db_session() as session:
        quiz_session = await session.get(TrophyQuizSession, quiz_id)
    return quiz_session.display_id if quiz_session else None


async def _send_trophy_reveal(interaction, quiz_id: str, decks: list, guesses: list, result: dict, revealed: bool = False, prefix: str = ""):
    """Send the ephemeral reveal + share-button reply for a scored submission.
    Shared by the Play button's already-submitted path and the guess view's
    Submit — both need to show a per-user private reveal."""
    lines = build_reveal_lines(decks, guesses, result, revealed=revealed)
    emoji_line = _build_emoji_line(result)
    display_id = await _load_display_id(quiz_id)
    total = apply_reveal_cost(result["total"], revealed)

    await interaction.response.send_message(
        prefix + "\n".join(lines),
        ephemeral=True,
        view=TrophyShareView(
            user=interaction.user,
            emoji_line=emoji_line,
            total_points=total,
            quiz_id=quiz_id,
            display_id=display_id,
        ),
    )


async def _show_existing_trophy_result(interaction, quiz_id: str, decks: list, submission: TrophyQuizSubmission):
    """Show a player's prior submission result (one-submission-per-player guard).

    Recompute the full result dict (incl. direction_points) via score_submission
    from the stored guesses/actual wins rather than storing it redundantly — it's
    deterministic and matches submission.points_earned.
    """
    result = score_submission(submission.guesses, [deck["wins"] for deck in decks])
    revealed = await has_revealed(quiz_id, submission.player_id)
    await _send_trophy_reveal(
        interaction, quiz_id, decks, submission.guesses, result,
        revealed=revealed,
        prefix="*(You already submitted this quiz)*\n",
    )


async def _handle_if_already_submitted(interaction, quiz_id: str, decks: list, user_id: str) -> bool:
    """One-submission-per-player guard shared by the Play button and the guess
    view's Submit: look up a prior submission and, if found, show its result.
    Returns True when the caller should stop (a result was already shown)."""
    async with db_session() as session:
        existing = await session.get(TrophyQuizSubmission, (quiz_id, user_id))
    if not existing:
        return False
    await _show_existing_trophy_result(interaction, quiz_id, decks, existing)
    return True


class TrophyQuizView(discord.ui.View):
    """
    Persistent public view on the trophy quiz message: a "Play" button that opens
    each player's own private ephemeral guess view, a "View Decklists" button, and
    the two MPT deck link buttons. Holds NO shared record dropdowns — shared
    selects on one public message collide across concurrent users; each player
    instead gets an isolated TrophyGuessView. Survives restarts (timeout=None).
    """

    def __init__(self, quiz_id: str, decks: list):
        super().__init__(timeout=None)  # Persistent across restarts
        self.quiz_id = quiz_id
        self.decks = decks

        for deck in decks:
            url = deck.get("mpt_url")
            if url:
                self.add_item(discord.ui.Button(
                    label=f"🔗 View Deck {deck['slot']}",
                    style=discord.ButtonStyle.link,
                    url=url,
                ))

    def to_metadata(self) -> dict:
        """Convert view properties to a dictionary for JSON storage (sticky message system)."""
        return {
            "quiz_id": self.quiz_id,
            "decks": self.decks,
            "view_type": "trophy_quiz",
        }

    @classmethod
    async def from_metadata(cls, bot, metadata: dict):
        """Recreate TrophyQuizView from stored metadata (sticky message system)."""
        quiz_id = metadata.get("quiz_id")
        decks = metadata.get("decks")
        if decks is None:
            async with db_session() as session:
                quiz_session = await session.get(TrophyQuizSession, quiz_id)
            decks = quiz_session.decks if quiz_session else []
        return cls(quiz_id=quiz_id, decks=decks)

    @discord.ui.button(
        label="🎯 Play",
        style=discord.ButtonStyle.success,
        custom_id="trophy_quiz_play",
    )
    async def play_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Open this player's private ephemeral guess view (or their prior result
        if they already submitted). Per-user ephemeral components never collide."""
        user_id = str(interaction.user.id)

        if await _handle_if_already_submitted(interaction, self.quiz_id, self.decks, user_id):
            return

        revealed = await has_revealed(self.quiz_id, user_id)
        content = "Guess each deck's record, then hit **Submit**:"
        if revealed:
            content = f"{_build_pilots_line(self.decks)}\n{content}"
        await interaction.response.send_message(
            content,
            view=TrophyGuessView(self.quiz_id, self.decks, interaction.user, revealed=revealed),
            ephemeral=True,
        )

    @discord.ui.button(
        label="View Decklists",
        style=discord.ButtonStyle.secondary,
        custom_id="trophy_quiz_view_decklists",
    )
    async def view_decklists_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Show both decks' full pools, ephemerally. Reads the STORED pool text
        (never re-renders via render_pool at view time)."""
        embed = discord.Embed(title="🃏 Decklists", color=discord.Color.blurple())
        for deck in self.decks:
            pool_text = deck.get("pool") or "*No pool available.*"
            embed.add_field(name=f"Deck {deck['slot']}", value=pool_text[:1024], inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


class TrophyGuessView(discord.ui.View):
    """Per-user ephemeral view: the two record dropdowns + Submit. Minted fresh
    per Play click, so each player has isolated selects and state — concurrent
    users never collide. Not persistent (5-minute timeout)."""

    def __init__(self, quiz_id: str, decks: list, user, revealed: bool = False):
        super().__init__(timeout=300)
        self.quiz_id = quiz_id
        self.decks = decks
        self.user = user
        self.revealed = revealed
        self.submitted = False  # set once this view's Submit succeeds (guards post-submit reveal)
        self.selections = {}  # {slot: guessed_wins} — one user, so keyed by slot
        # Serializes Reveal/Submit on this view instance so the two handlers never
        # interleave: a concurrent Reveal-then-Submit (or double-click Submit) can
        # otherwise dodge the reveal penalty or hit a composite-PK IntegrityError.
        self._lock = asyncio.Lock()

        for i, deck in enumerate(decks):
            self.add_item(TrophyRecordSelect(slot=deck["slot"], parent_view=self, row=i))

        if revealed:
            for child in self.children:
                if getattr(child, "custom_id", None) == "trophy_quiz_reveal_names":
                    child.disabled = True

    @discord.ui.button(
        label=f"🔎 Reveal names (−{REVEAL_COST} pts)",
        style=discord.ButtonStyle.secondary,
        custom_id="trophy_quiz_reveal_names",
        row=2,
    )
    async def reveal_names_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Pay REVEAL_COST (applied on submit): persist the reveal (so re-opening
        remembers it), then show both decks' pilots privately."""
        # Locked against submit_button on this same view instance: without this,
        # a concurrent Reveal + Submit (or double-click) could interleave —
        # Submit reading has_revealed()/self.submitted before this handler's
        # commit lands, dodging the penalty or desyncing the displayed score.
        async with self._lock:
            # Post-submit, revealing is moot (the result already shows the pilots) and
            # must not record a reveal — that would desync the displayed total from the
            # already-stored, unpenalized points_earned. A submitted player can only reach
            # this button on the same view they submitted from (Play blocks a new one), so
            # an in-memory flag suffices. Show names, no charge.
            note = "" if self.submitted else f"\n*(−{REVEAL_COST} points will apply when you submit.)*"
            if not self.submitted:
                await record_reveal(self.quiz_id, str(interaction.user.id))
                self.revealed = True
            button.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                f"{_build_pilots_line(self.decks)}{note}",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.success,
        custom_id="trophy_quiz_guess_submit",
        row=2,
    )
    async def submit_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Validate both records chosen, score, persist (once per player), and
        reply ephemerally with the reveal + a share button."""
        # Locked against reveal_names_button on this same view instance — see
        # that handler's comment for the interleaving this prevents.
        async with self._lock:
            user_id = str(interaction.user.id)

            # One submission per player: short-circuit if one already exists.
            if await _handle_if_already_submitted(interaction, self.quiz_id, self.decks, user_id):
                return

            slots = [deck["slot"] for deck in self.decks]
            if any(slot not in self.selections for slot in slots):
                await interaction.response.send_message(
                    "Please select a record for both decks before submitting!",
                    ephemeral=True,
                )
                return

            guesses = [self.selections[slot] for slot in slots]
            result = score_submission(guesses, [deck["wins"] for deck in self.decks])
            revealed = await has_revealed(self.quiz_id, user_id)
            final_points = apply_reveal_cost(result["total"], revealed)

            async with db_session() as session:
                submission = TrophyQuizSubmission(
                    quiz_id=self.quiz_id,
                    player_id=user_id,
                    display_name=get_display_name(interaction.user),
                    guesses=guesses,
                    direction_correct=result["direction_correct"],
                    exact_points=result["exact_points"],
                    points_earned=final_points,
                )
                session.add(submission)

                await session.execute(
                    update(TrophyQuizSession)
                    .where(TrophyQuizSession.quiz_id == self.quiz_id)
                    .values(total_participants=TrophyQuizSession.total_participants + 1)
                )

                await session.commit()

            logger.info(f"User {user_id} submitted trophy quiz {self.quiz_id}: {final_points} points (revealed={revealed})")

            self.submitted = True  # guard: a post-submit reveal click on this view must not record/charge
            await _send_trophy_reveal(interaction, self.quiz_id, self.decks, guesses, result, revealed=revealed)
