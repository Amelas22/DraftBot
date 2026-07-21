import asyncio

import discord
from loguru import logger
from sqlalchemy import update
from database.db_session import db_session
from models import TrophyQuizSession, TrophyQuizSubmission
from sqlalchemy.exc import IntegrityError
from services.trophy_quiz_service import score_submission, record_label, CHANGE_COST, apply_change_cost
from helpers.display_names import get_display_name
from helpers.quiz_threads import post_quiz_share

# Record dropdown options: (wins, label). Values are the win count as a string
# ("3".."0"), matched against services.trophy_quiz_service record semantics.
RECORD_OPTIONS = [(3, "3-0"), (2, "2-1"), (1, "1-2"), (0, "0-3")]


def build_reveal_lines(decks, guesses, result, changed: bool = False) -> list:
    """Per-deck reveal + a better-deck/points summary line.

    decks: the 2 stored deck dicts (slot/drafter_id/wins).
    guesses: [winsA, winsB] the player's FINAL submitted guess.
    result: the dict returned by services.trophy_quiz_service.score_submission.
    changed: when True, appends a paid-change penalty line and the penalized final.
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
    if changed:
        final = apply_change_cost(result["total"], changed)
        lines.append(f"🔎 Changed answer after seeing names (−{CHANGE_COST}) → **{final} pts**")
    return lines


def _build_pilots_line(decks: list) -> str:
    """The "🔎 Piloted by — Deck A: @x, Deck B: @y" line shown when names are
    revealed (after the initial guess) alongside the Keep/Change choice."""
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

    def __init__(self, slot: str, parent_view, row: int = None, default_wins: int = None):
        self.slot = slot
        self.parent_view = parent_view

        options = [
            discord.SelectOption(label=label, value=str(wins), default=(wins == default_wins))
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


async def _get_submission(quiz_id: str, user_id: str):
    async with db_session() as session:
        return await session.get(TrophyQuizSubmission, (quiz_id, user_id))


async def _persist_pending(quiz_id: str, user_id: str, display_name: str, guesses: list) -> bool:
    """Persist the player's initial guess as a PENDING (unfinalized) submission the
    instant they first submit — so re-opening the quiz resumes the Keep/Change
    choice rather than allowing a free re-guess after names are revealed.

    Returns True if a new pending row was created, False if a submission already
    exists (a concurrent double-submit or a resume)."""
    async with db_session() as session:
        session.add(TrophyQuizSubmission(
            quiz_id=quiz_id,
            player_id=user_id,
            display_name=display_name,
            guesses=guesses,
            direction_correct=False,
            exact_points=[0, 0],
            points_earned=0,
            finalized=False,
            changed_answer=False,
        ))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()  # already have a (pending or finalized) row
            return False


async def _finalize(quiz_id: str, user_id: str, final_guesses: list, changed: bool, decks: list) -> bool:
    """Transition the player's pending submission to finalized with their committed
    guess. Idempotent: only the first finalize takes effect (and increments
    total_participants). Returns True if THIS call finalized it, False if it was
    already finalized (double-click / race)."""
    result = score_submission(final_guesses, [deck["wins"] for deck in decks])
    points = apply_change_cost(result["total"], changed)
    async with db_session() as session:
        # Atomic transition: the conditional UPDATE only matches a not-yet-finalized
        # row, so exactly one caller wins even across separate view instances (two
        # decide views, or a decide + change view). The participant increment is
        # gated on that win, so it happens exactly once — the per-view lock alone
        # can't guarantee this because the racing handlers live on different views.
        res = await session.execute(
            update(TrophyQuizSubmission)
            .where(
                TrophyQuizSubmission.quiz_id == quiz_id,
                TrophyQuizSubmission.player_id == user_id,
                TrophyQuizSubmission.finalized.is_(False),
            )
            .values(
                guesses=final_guesses,
                direction_correct=result["direction_correct"],
                exact_points=result["exact_points"],
                points_earned=points,
                changed_answer=changed,
                finalized=True,
            )
        )
        if res.rowcount == 0:
            return False  # no pending row, or another call already finalized it
        await session.execute(
            update(TrophyQuizSession)
            .where(TrophyQuizSession.quiz_id == quiz_id)
            .values(total_participants=TrophyQuizSession.total_participants + 1)
        )
        await session.commit()
    return True


async def _send_final_reveal(interaction, quiz_id: str, decks: list, guesses: list, changed: bool, prefix: str = ""):
    """Send the full ephemeral result (records + score + optional −2 line) plus a
    Share button, for a finalized submission."""
    result = score_submission(guesses, [deck["wins"] for deck in decks])
    lines = build_reveal_lines(decks, guesses, result, changed=changed)
    emoji_line = _build_emoji_line(result)
    display_id = await _load_display_id(quiz_id)
    total = apply_change_cost(result["total"], changed)
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


async def _reveal_if_finalized(interaction, quiz_id: str, decks: list, sub) -> bool:
    """If `sub` is a finalized submission, show its committed result (with the
    "already submitted" prefix) and return True so the caller stops. Returns
    False when there's nothing to show (no submission, or still pending)."""
    if sub is None or not sub.finalized:
        return False
    await _send_final_reveal(
        interaction, quiz_id, decks, sub.guesses, sub.changed_answer,
        prefix="*(You already submitted this quiz)*\n",
    )
    return True


async def _reveal_names_and_decide(interaction, quiz_id: str, decks: list, user, initial_guesses: list):
    """Reveal ONLY the pilots' names (not records/score) and present the
    Keep / Pay-2-to-change choice on the player's locked initial guess."""
    content = (
        f"{_build_pilots_line(decks)}\n"
        f"Now that you know who piloted each deck, keep your answer or pay "
        f"**{CHANGE_COST} points** to change it."
    )
    await interaction.response.send_message(
        content,
        view=TrophyDecideView(quiz_id, decks, user, initial_guesses),
        ephemeral=True,
    )


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
        """Open the player's flow: fresh guess (no submission), resume the
        Keep/Change choice (pending submission), or show the result (finalized)."""
        user_id = str(interaction.user.id)
        sub = await _get_submission(self.quiz_id, user_id)
        if await _reveal_if_finalized(interaction, self.quiz_id, self.decks, sub):
            return
        if sub is not None:  # pending: resume on the locked initial guess
            await _reveal_names_and_decide(interaction, self.quiz_id, self.decks, interaction.user, sub.guesses)
            return
        await interaction.response.send_message(
            "Guess each deck's record, then hit **Submit**:",
            view=TrophyGuessView(self.quiz_id, self.decks, interaction.user),
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
    """Per-user ephemeral view: two record dropdowns + Submit. Minted fresh per
    Play (initial mode) or per Pay-to-change (change mode, dropdowns pre-filled
    with the initial guess). Isolated per user; not persistent (5-minute timeout).

    initial_guesses=None -> INITIAL mode: Submit persists the pending row and
    reveals names. initial_guesses set -> CHANGE mode: Submit finalizes with the
    revised guess, charging CHANGE_COST iff it differs from the initial."""

    def __init__(self, quiz_id: str, decks: list, user, initial_guesses: list = None):
        super().__init__(timeout=300)
        self.quiz_id = quiz_id
        self.decks = decks
        self.user = user
        self.initial_guesses = initial_guesses  # None = initial mode; set = change mode
        self.selections = {}  # {slot: guessed_wins} — one user, so keyed by slot
        # Serialize a user's own Submit clicks on this view instance (double-click).
        self._lock = asyncio.Lock()

        # In change mode, pre-fill each dropdown with the player's initial guess.
        prefill = None
        if initial_guesses is not None:
            prefill = {deck["slot"]: initial_guesses[i] for i, deck in enumerate(decks)}
            self.selections = dict(prefill)
        for i, deck in enumerate(decks):
            self.add_item(TrophyRecordSelect(
                slot=deck["slot"], parent_view=self, row=i,
                default_wins=prefill.get(deck["slot"]) if prefill else None,
            ))

    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.success,
        custom_id="trophy_quiz_guess_submit",
        row=2,
    )
    async def submit_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        async with self._lock:
            user_id = str(interaction.user.id)
            slots = [deck["slot"] for deck in self.decks]
            if any(slot not in self.selections for slot in slots):
                await interaction.response.send_message(
                    "Please select a record for both decks before submitting!",
                    ephemeral=True,
                )
                return
            guesses = [self.selections[slot] for slot in slots]

            if self.initial_guesses is None:
                # INITIAL submit: persist pending + reveal names + decide.
                created = await _persist_pending(
                    self.quiz_id, user_id, get_display_name(interaction.user), guesses)
                if not created:
                    # A submission already exists (concurrent double-submit or a
                    # resume). Route to the right state rather than duplicate.
                    sub = await _get_submission(self.quiz_id, user_id)
                    if await _reveal_if_finalized(interaction, self.quiz_id, self.decks, sub):
                        return
                    initial = sub.guesses if sub is not None else guesses
                    await _reveal_names_and_decide(
                        interaction, self.quiz_id, self.decks, interaction.user, initial)
                    return
                logger.info(f"User {user_id} made an initial trophy guess on {self.quiz_id}")
                await _reveal_names_and_decide(
                    interaction, self.quiz_id, self.decks, interaction.user, guesses)
            else:
                # CHANGE submit: finalize with the revised guess (charge iff changed).
                changed = guesses != self.initial_guesses
                did = await _finalize(self.quiz_id, user_id, guesses, changed, self.decks)
                if not did:
                    # already finalized elsewhere — show the committed result (guard
                    # sub None defensively, though a change-mode view always has a row)
                    sub = await _get_submission(self.quiz_id, user_id)
                    final_guesses = sub.guesses if sub is not None else guesses
                    final_changed = sub.changed_answer if sub is not None else changed
                    await _send_final_reveal(
                        interaction, self.quiz_id, self.decks, final_guesses,
                        final_changed, prefix="*(You already submitted this quiz)*\n")
                    return
                logger.info(
                    f"User {user_id} finalized trophy quiz {self.quiz_id} via change "
                    f"(changed={changed})")
                await _send_final_reveal(interaction, self.quiz_id, self.decks, guesses, changed)


class TrophyDecideView(discord.ui.View):
    """Ephemeral Keep / Pay-2-to-change choice shown after names are revealed.
    Operates on the player's locked initial guess (persisted as a pending row)."""

    def __init__(self, quiz_id: str, decks: list, user, initial_guesses: list):
        super().__init__(timeout=300)
        self.quiz_id = quiz_id
        self.decks = decks
        self.user = user
        self.initial_guesses = initial_guesses
        self._lock = asyncio.Lock()

    @discord.ui.button(
        label="Keep my answer",
        style=discord.ButtonStyle.success,
        custom_id="trophy_quiz_keep",
    )
    async def keep_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        async with self._lock:
            user_id = str(interaction.user.id)
            did = await _finalize(self.quiz_id, user_id, self.initial_guesses, False, self.decks)
            prefix = "" if did else "*(You already submitted this quiz)*\n"
            sub = await _get_submission(self.quiz_id, user_id)
            guesses = sub.guesses if sub is not None else self.initial_guesses
            changed = sub.changed_answer if sub is not None else False
            await _send_final_reveal(interaction, self.quiz_id, self.decks, guesses, changed, prefix=prefix)

    @discord.ui.button(
        label=f"🔎 Pay {CHANGE_COST} to change",
        style=discord.ButtonStyle.secondary,
        custom_id="trophy_quiz_change",
    )
    async def change_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        async with self._lock:
            user_id = str(interaction.user.id)
            sub = await _get_submission(self.quiz_id, user_id)
            if await _reveal_if_finalized(interaction, self.quiz_id, self.decks, sub):
                return
            await interaction.response.send_message(
                f"Revise your records, then **Submit** (−{CHANGE_COST} points if you change anything):",
                view=TrophyGuessView(self.quiz_id, self.decks, interaction.user,
                                     initial_guesses=self.initial_guesses),
                ephemeral=True,
            )
