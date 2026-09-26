"""Tests for the staked draft's signup board.

One full-width field, one row per player: the entry amount right-aligned in an
inline code span, then crowns and the name outside it. The span is what aligns
the amounts -- equal character counts render equal widths in monospace -- and
being one field is what makes it survive mobile, where Discord stacks inline
fields instead of placing them side by side.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from helpers.signup_board import build_board, shown_stake, signup_header, signup_rows


# ---- the header ------------------------------------------------------------

def test_an_empty_queue_says_so_rather_than_counting_to_zero():
    assert signup_header(0) == "No players yet."


def test_the_header_counts_the_queue():
    assert signup_header(6) == "**6 players**"


def test_one_player_is_not_one_players():
    assert signup_header(1) == "**1 player**"


def test_the_header_names_the_pool_when_there_is_one():
    assert signup_header(6, pool=300) == "**6 players** · prize pool: up to 300 tix"


def test_a_pool_of_nothing_is_not_advertised():
    assert signup_header(3, pool=0) == "**3 players**"


# ---- the rows --------------------------------------------------------------

def test_the_amount_leads_and_the_name_follows_it():
    """The numbers have to lead. Anything whose rendered width cannot be known
    here -- an emoji, a name long enough to wrap -- must come last so its drift
    never reaches the column. Same rule as _standings_rows."""
    rows = signup_rows({"1": "Bryan"}, {"1": _stake(50)}, _stored_name)

    assert rows == ["`50` **Bryan**"]


def test_amounts_are_left_justified_but_padded_to_a_common_width():
    """Left-justified reads better, and it costs nothing: the span is still
    PADDED to a common width, so equal character counts still render equal
    widths in monospace and every name still starts at the same x."""
    rows = signup_rows({"1": "Bryan", "2": "Ava", "3": "Dev"},
                       {"1": _stake(50), "2": _stake(300), "3": _stake(20)},
                       _stored_name)

    assert rows == ["`50  ` **Bryan**", "`100+` **Ava**", "`20  ` **Dev**"]


def test_a_pod_with_no_large_entries_gets_a_narrow_column():
    """Width is measured from the rows actually shown rather than fixed, so a
    small-stakes queue does not carry four characters of padding."""
    rows = signup_rows({"1": "Bryan", "2": "Ava"},
                       {"1": _stake(50), "2": _stake(5)}, _stored_name)

    assert rows == ["`50` **Bryan**", "`5 ` **Ava**"]


def test_crowns_sit_outside_the_span_so_their_art_survives():
    """The whole reason this is an inline span and not a fenced block: markdown
    and custom emoji still render outside it. Two of the five crown tiers are
    custom Discord emoji, which a code block would print as raw shortcodes."""
    rows = signup_rows({"1": "Mack"}, {"1": _stake(50)},
                       lambda uid, stored: f"<:doublecrown:146> {stored}")

    assert rows == ["`50` **<:doublecrown:146> Mack**"]


def test_somebody_who_has_not_set_an_entry_gets_a_dash():
    """A dash keeps the column aligned where an emoji could not -- an emoji's
    width in the span is unknowable, and one wide cell shifts its whole row."""
    rows = signup_rows({"1": "Bryan", "2": "Ava"}, {"1": _stake(100)}, _stored_name)

    assert rows == ["`100+` **Bryan**", "`-   ` **Ava**"]


def test_a_large_entry_shows_only_that_it_is_large():
    """Above the ceiling every entry reads the same, so the top of the table
    cannot be ranked."""
    rows = signup_rows({"1": "Dev", "2": "Eli"},
                       {"1": _stake(300), "2": _stake(100)}, _stored_name)

    assert rows == ["`100+` **Dev**", "`100+` **Eli**"]
    assert "300" not in "".join(rows)


def test_players_stay_in_join_order():
    """Never sorted by size. Sorting is what made this a leaderboard before,
    with the largest entry at the top of every draft."""
    rows = signup_rows({"1": "Ava", "2": "Dev", "3": "Cora"},
                       {"1": _stake(20), "2": _stake(300), "3": _stake(20)},
                       _stored_name)

    assert [r.split("`")[2].strip(" *") for r in rows] == ["Ava", "Dev", "Cora"]


def test_an_empty_queue_has_no_rows():
    assert signup_rows({}, {}, _stored_name) == []


# ---- the whole field -------------------------------------------------------

def test_the_board_is_the_header_then_the_rows():
    board = build_board({"1": "Ava", "2": "Ben"},
                        {"1": _stake(20), "2": _stake(300)}, _stored_name, pool=40)

    assert board == ("**2 players** · prize pool: up to 40 tix\n"
                     "`20  ` **Ava**\n"
                     "`100+` **Ben**")


def test_an_empty_queue_is_the_header_alone():
    assert build_board({}, {}, _stored_name) == "No players yet."


# ---- the live render path --------------------------------------------------

def _stub_db(stake_rows):
    results = MagicMock()
    results.scalars.return_value.all.return_value = stake_rows
    db_session = MagicMock()
    db_session.execute = AsyncMock(return_value=results)
    db_session.begin.return_value.__aenter__ = AsyncMock(return_value=db_session)
    db_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


async def _render(embed, sign_ups, guild=None):
    """Drive update_draft_message over a staked session and return the embed it
    edited onto the message."""
    import views

    session = SimpleNamespace(
        session_id="123456789012345678-1753500000",
        friendly_id="lightning-bolt-7", cube="LSVCube", guild_id="999",
        draft_channel_id="111", message_id="222", session_type="staked",
        sign_ups=sign_ups, packs_per_player=3, cards_per_pack=15,
    )
    message = MagicMock()
    message.embeds = [embed]
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.guild = guild
    bot = MagicMock()
    bot.get_channel.return_value = channel
    stakes = [SimpleNamespace(player_id=uid, max_stake=amount, is_capped=True)
              for uid, amount in (("1", 20), ("2", 300))]

    with (patch.object(views, "get_draft_session", AsyncMock(return_value=session)),
          patch.object(views, "AsyncSessionLocal", _stub_db(stakes)),
          patch("services.draft_pool_service.contributions",
                AsyncMock(return_value={"1": 20, "2": 300})),
          patch("services.draft_pool_service.max_pool", return_value=40)):
        await views.update_draft_message(bot, session.session_id)

    assert message.edit.await_args, "update_draft_message never edited the message"
    return message.edit.await_args.kwargs["embed"]


@pytest.mark.asyncio
async def test_a_staked_draft_renders_one_aligned_field():
    """End to end: the field the bot actually edits onto the live message."""
    embed = discord.Embed(title="Prize Pool Draft!")
    embed.add_field(name="Cube:", value="[LSVCube](https://x)", inline=True)
    embed.add_field(name="Sign-Ups:", value="**Players (0):**\nNo players yet.", inline=False)

    edited = await _render(embed, {"1": "Ava", "2": "Ben"})

    board = [(f.name, f.value, f.inline) for f in edited.fields
             if f.name.startswith("Sign-Ups")]
    assert board == [("Sign-Ups:",
                      "**2 players** · prize pool: up to 40 tix\n"
                      "`20  ` **Ava**\n"
                      "`100+` **Ben**", False)]


@pytest.mark.asyncio
async def test_the_board_never_grows_onto_an_embed_that_has_no_signup_field():
    """After teams form, team_creator replaces the queue embed with the "Draft
    is Ready!" one, which has no Sign-Ups field. A player still holding the
    5-minute ephemeral stake view can submit after that and land back in
    update_draft_message -- which must not re-list the queue and its entries
    onto the ready message.
    """
    ready = discord.Embed(title="Draft is Ready!")
    ready.add_field(name="Team A", value="Ava", inline=True)

    edited = await _render(ready, {"1": "Ava", "2": "Ben"})

    assert not any(f.name.startswith("Sign-Ups") for f in edited.fields), (
        f"the queue board regrew onto the ready embed: {[f.name for f in edited.fields]}")


def _stored_name(user_id, stored):
    return stored


def _stake(amount, is_capped=True):
    """The shape views.py builds from a StakeInfo row. is_capped is carried so
    the fixture matches the real dict; the board never reads it -- capping is
    settled at team creation, not on the queue."""
    return {"amount": amount, "is_capped": is_capped}


def test_a_board_too_long_for_one_field_says_so(capsys):
    """views.py splits a long board across continuation fields, but its
    update_field cannot CREATE a field, so those chunks are dropped and the
    queue renders short. Unreachable at draft pod sizes; the warning is here so
    that if it ever is reached, it is not silent."""
    import sys

    from loguru import logger

    hid = logger.add(sys.stderr, level="WARNING")
    try:
        build_board({str(i): f"Player{i:03d}" for i in range(60)},
                    {str(i): _stake(20) for i in range(60)}, _stored_name)
        assert "over the 1000-char field split threshold" in capsys.readouterr().err
    finally:
        logger.remove(hid)


# ---- names that could forge or break the row --------------------------------

def test_a_name_that_would_break_the_bold_is_left_unbolded():
    """discord.utils.escape_markdown defaults to ignore_links=True, so a
    URL-shaped name keeps its markdown: "https://x.y/**oops" arrives already
    carrying ** and would close the bold this row opens. An unclosed delimiter
    does not stop at its own row -- it bleeds into every row under it -- so a
    name we cannot safely wrap goes unwrapped instead."""
    rows = signup_rows({"1": "https://x.y/**oops"}, {"1": _stake(20)}, _stored_name)

    assert rows == ["`20` https://x.y/**oops"], rows


def test_a_name_ending_in_a_backslash_is_left_unbolded():
    """The backslash would escape the closing ** and run the bold on."""
    rows = signup_rows({"1": "trailing\\"}, {"1": _stake(20)}, _stored_name)

    assert rows == ["`20` trailing\\"], rows


def test_an_ordinary_name_is_still_bolded():
    """The guard must not cost the weight on every other row."""
    assert signup_rows({"1": "Ava"}, {"1": _stake(20)}, _stored_name) == ["`20` **Ava**"]


def _guild_with_crowned(user_id, role_name):
    """A guild whose member carries a crown role, so the render goes through
    get_display_name for real rather than the stored-name fallback."""
    role = MagicMock()
    role.name = role_name
    member = MagicMock()
    member.roles = [role]
    member.display_name = "Bryan"
    guild = MagicMock()
    guild.id = 1234
    guild.get_member.return_value = member
    return guild


@pytest.mark.asyncio
async def test_a_crowned_player_is_decorated_through_the_real_lookup():
    """The other integration tests pass guild=None, which takes the stored-name
    fallback and never touches get_display_name -- so a wiring error on
    decorated names would not show up. This drives the member lookup."""
    embed = discord.Embed(title="Prize Pool Draft!")
    embed.add_field(name="Sign-Ups:", value="No players yet.", inline=False)
    guild = _guild_with_crowned("1", "Crown")

    with patch("config.get_config", return_value={
            "crown_roles": {"enabled": True, "role_names": {"1": "Crown"}}}):
        edited = await _render(embed, {"1": "stored-not-used"}, guild=guild)

    field = next(f for f in edited.fields if f.name == "Sign-Ups:")
    assert "👑 Bryan" in field.value, field.value


# ---- what an entry may reveal ----------------------------------------------

def test_a_small_entry_shows_its_exact_amount():
    assert shown_stake(20) == "20"


def test_the_ceiling_boundary_is_exact():
    """One below the ceiling is a figure, the ceiling itself is the bucket. The
    only edge here that can silently regress -- a `>` for a `>=` moves it by one
    and nothing else in the suite would notice."""
    assert shown_stake(99) == "99"
    assert shown_stake(100) == "100+"


def test_an_entry_above_the_ceiling_never_shows_its_own_figure():
    """Somebody in for 300 and somebody in for 100 must be indistinguishable,
    so the top of the table cannot be ranked."""
    assert shown_stake(300) == "100+"


def test_an_unreadable_amount_does_not_take_the_board_down():
    assert shown_stake(None) == "?"
    assert shown_stake("banana") == "?"
