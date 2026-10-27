"""A premade draft's sign-up panel says up front when joining costs tix.

The fee is only enforced when someone presses a team button, so a panel that
does not mention it lets players find out from a refusal.
"""
from types import SimpleNamespace

import discord

from sessions.premade_session import PremadeSession


def _details(**overrides):
    details = dict(
        session_id="123456789012345678-1753500000",
        draft_id="A7K2M9QZ",
        friendly_id="lightning-bolt-7",
        cube_choice="LSVCube",
        draft_start_time=1753500000,
        packs_per_player=3,
        cards_per_pack=15,
        guild_id="1",
        team_a_name=None,
        team_b_name=None,
        entry_fee=None,
    )
    details.update(overrides)
    return SimpleNamespace(**details)


def _embed(**overrides):
    return PremadeSession(_details(**overrides), session_factory=lambda: None).create_embed()


def test_an_entry_fee_draft_names_the_fee_in_its_title():
    embed = _embed(entry_fee=50)
    assert "50 tix entry" in (embed.title or "")


def test_an_entry_fee_draft_explains_the_fee_before_the_steps():
    description = _embed(entry_fee=50).description or ""
    fee_at = description.index("**Entry fee: 50 tix**")
    assert fee_at < description.index("How to use bot")
    assert "Joining a team takes **50 tix** from your wallet" in description
    assert "Leaving your team refunds it" in description
    assert "winning team gets back double" in description
    assert "even" in description


def test_an_entry_fee_draft_is_gold_like_a_prize_pool_queue():
    assert _embed(entry_fee=50).colour == discord.Color.gold()


def test_a_free_premade_draft_reads_as_before():
    for fee in (None, 0):
        embed = _embed(entry_fee=fee)
        assert "tix" not in (embed.title or "")
        assert "Entry fee" not in (embed.description or "")
        assert embed.colour == discord.Color.blue()
