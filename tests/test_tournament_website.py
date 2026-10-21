"""A tournament's league site: a mod links it once, and every board links to it."""
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant
from services.tournament_formatter import (
    create_bracket_embed,
    create_registration_embed,
    create_standings_embed,
    pairings_header,
    validate_website_url,
)
from services.tournament_service import BracketRow

SITE = "https://magic-draft-logs.nyc3.digitaloceanspaces.com/league-drafts/season-2/index.html"
LINE = f"🌐 [View teams, standings and pairings on the league site]({SITE})"


def _tournament(website_url=None, status="registration"):
    return Tournament(id=1, guild_id="g1", name="Lotus Open", total_rounds=3, current_round=1,
                      format="swiss", status=status, entry_fee=0,
                      payout_structure="winner_take_all", website_url=website_url)


def _team():
    return TournamentParticipant(id=1, tournament_id=1, team_id=1, team_name="Alpha",
                                 captain_user_id="10", status="paid")


def _bracket(website_url=None):
    row = BracketRow(1, "Final", (1, "Alpha"), (2, "Beta"), None, None, None, None, None, False)
    return create_bracket_embed("Lotus Open", [row], website_url)


@pytest.mark.parametrize("build", [
    lambda url: create_registration_embed(_tournament(url), [_team()]),
    lambda url: create_registration_embed(_tournament(url), []),   # empty board, no description
    lambda url: create_standings_embed(_tournament(url, status="active"), []),
    _bracket,
], ids=["registration", "empty-registration", "standings", "bracket"])
def test_every_board_links_the_site_only_when_one_is_set(build):
    linked = build(SITE)
    assert linked.url == SITE
    assert linked.description.endswith(LINE)

    plain = build(None)
    assert not plain.url
    assert "🌐" not in (plain.description or "")


def test_the_link_follows_what_the_board_already_says():
    embed = create_standings_embed(_tournament(SITE, status="active"), [])
    assert embed.description.startswith("**Status:** Active")
    assert f"\n\n{LINE}" in embed.description


def test_pairings_header_links_the_site_without_a_preview_card():
    assert pairings_header("Week 2", None) == "**Week 2 pairings:**"
    assert pairings_header("Week 2", SITE) == f"**Week 2 pairings:** · [League site](<{SITE}>)"
    assert pairings_header("Week 2", SITE, " — later") == (
        f"**Week 2 pairings:** · [League site](<{SITE}>) — later")


def test_a_website_must_be_a_plain_https_link():
    assert validate_website_url(f"  {SITE} ") == SITE
    assert validate_website_url(None) is None and validate_website_url("  ") is None
    for bad in ("http://example.com", "example.com", "https://", "https://a.com/x y", "https://a.com/x\ty",
                "https://a.com/(x)", "https://a.com/" + "x" * 512):
        with pytest.raises(ValueError):
            validate_website_url(bad)


async def _seed():
    async with db_session() as session:
        t = Tournament(guild_id="123", name="Lotus Open", total_rounds=0, format="manual",
                       status="registration", entry_fee=0)
        session.add(t)
        await session.flush()
        return t.id


async def _set_website(url):
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    redraws = {name: AsyncMock() for name in
               ("refresh_boards", "update_standings_message", "update_bracket_message")}
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch.multiple("cogs.tournament_commands", **redraws):
        await TournamentCog.set_website.callback(cog, ctx, url=url)
    return ctx.followup.send.await_args.args[0], redraws


async def _stored(t_id):
    async with db_session() as session:
        return (await session.get(Tournament, t_id)).website_url


@pytest.mark.asyncio
async def test_set_website_stores_the_link_and_redraws_every_board(test_db):  # noqa: F811
    t_id = await _seed()

    reply, redraws = await _set_website(SITE)

    assert await _stored(t_id) == SITE
    assert reply.startswith("✅")
    redraws["refresh_boards"].assert_awaited_once_with(ANY, [t_id])
    redraws["update_standings_message"].assert_awaited_once()
    redraws["update_bracket_message"].assert_awaited_once()


@pytest.mark.asyncio
async def test_set_website_without_a_link_removes_it(test_db):  # noqa: F811
    t_id = await _seed()
    await _set_website(SITE)

    reply, _ = await _set_website(None)

    assert await _stored(t_id) is None
    assert "Removed" in reply


@pytest.mark.asyncio
async def test_a_bad_link_is_refused_and_changes_nothing(test_db):  # noqa: F811
    t_id = await _seed()
    await _set_website(SITE)

    reply, redraws = await _set_website("http://example.com")

    assert reply.startswith("❌")
    assert await _stored(t_id) == SITE
    redraws["refresh_boards"].assert_not_awaited()


def test_set_website_is_admin_only():
    from cogs.tournament_commands import TournamentCog
    from helpers.permissions import is_bot_manager

    assert is_bot_manager in TournamentCog.set_website.checks
