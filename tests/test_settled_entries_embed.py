"""A staked draft's embed: that the pool appears at all, and that it is real.

Two prod bugs, one masking the other, both landed in the prize-pool migration
(2ab11bf):

1. `_add_stake_info_to_embed` was gated on a `stake_info_by_player` dict that
   nothing populated any more, so it returned early and the field vanished from
   every staked draft's embed.
2. Behind that dead gate, a parser re-derived the regime by splitting each line
   on " vs ". The pool renders one line per player (`**Alice**: 100 tix`) where
   the retired tiered matcher rendered pairs (`Alice vs Bob: 100 tix`), so with
   the gate open it raised IndexError.

The dead gate hid the broken parser, so the symptom was a missing field rather
than a crash -- and a missing field reads as "this draft has no entries".

And a third thing, which is what a player actually notices: the figure. The
embed used to be built inside the team-creation transaction, necessarily before
match_pool could run, so it stated what everyone DECLARED. A draft that declared
860 and played for 220 announced 860 and never corrected it.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from services import team_creator


def _session():
    return SimpleNamespace(session_id="s1", sign_ups={"1": "Alice", "2": "Bob"})


def _field(embed):
    return next((f for f in embed.fields if "Prize Pool" in f.name), None)


async def _render(lines, total):
    embed = discord.Embed(title="t")
    with patch.object(team_creator, "get_formatted_stake_pairs",
                      AsyncMock(return_value=(lines, total))):
        await team_creator._add_stake_info_to_embed(embed, _session())
    return embed


@pytest.mark.asyncio
async def test_the_pool_reaches_the_embed():
    """One line per player is the live format, and it must render."""
    embed = await _render(["**Alice**: 100 tix", "**Bob**: 60 tix"], 160)

    field = _field(embed)
    assert field is not None, "the field vanished -- the gate is dead again"
    assert "**Alice**: 100 tix" in field.value
    assert "160 tix" in field.name


@pytest.mark.asyncio
async def test_a_retired_tiered_draft_still_renders():
    """Drafts paired under the old matcher keep their pairings until they
    finish, and they come through the same formatter."""
    embed = await _render(["**Alice** vs **Bob**: 100 tix"], 100)

    assert "**Alice** vs **Bob**: 100 tix" in _field(embed).value


@pytest.mark.asyncio
async def test_a_player_named_like_a_pairing_is_left_alone():
    """The regime used to be guessed from the text of the line, so a display
    name containing " vs " was read as two players and its markdown mangled
    (`****Alice** vs **Bob****: 100 tix`). Nothing parses the line now."""
    embed = await _render(["**Alice vs Bob**: 100 tix"], 100)

    assert "**Alice vs Bob**: 100 tix" in _field(embed).value


@pytest.mark.asyncio
async def test_nothing_is_added_when_there_is_nothing_at_risk():
    assert not (await _render([], 0)).fields


# The ordering is the feature, and create_and_display_teams is a 250-line
# coroutine over a live interaction -- the existing guards on it
# (test_draft_pool_matching) read the parsed tree rather than run it. The e2e
# harness drives the real thing; this pins the shape that makes it possible.

def _team_creation_ast():
    import ast
    import inspect

    from services.team_creator import create_and_display_teams

    return ast.parse(inspect.getsource(create_and_display_teams)).body[0]


def _calls_named(node, name):
    import ast

    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def test_the_staked_draft_is_announced_after_its_pool_settles():
    """This is the whole change. Announce first and the embed states the
    declared total, which is not what anybody plays for; there is then nothing
    that ever corrects it short of the victory post.

    Lexical order is the real guarantee here -- both calls sit in the same
    straight-line block after the transaction, with no branch between them.
    """
    import ast

    fn = _team_creation_ast()
    settle = _calls_named(fn, "match_pool")
    announce = _calls_named(fn, "_handle_staked_draft_completion")
    assert settle, "team creation never closes the book"
    assert announce, "the staked draft is never announced"
    assert max(n.lineno for n in settle) < min(n.lineno for n in announce), (
        "the staked draft is announced before match_pool settles its pool, so "
        "the embed states what players declared rather than what they play for")


def test_the_staked_announcement_is_outside_the_team_creation_transaction():
    """It cannot be inside and still be correct -- match_pool runs after the
    commit, so anything built before it reads an unsettled ledger. It also
    means two Discord round trips no longer happen while this transaction
    holds SQLite's single write lock.
    """
    import ast

    fn = _team_creation_ast()
    transactions = [n for n in ast.walk(fn)
                    if isinstance(n, (ast.With, ast.AsyncWith))
                    and "begin" in ast.dump(n.items[0].context_expr)]
    assert transactions, "the team-creation transaction moved; re-check this guard"
    inside = [t for t in transactions
              if _calls_named(t, "_handle_staked_draft_completion")]
    assert inside == [], (
        "the staked draft is announced from inside the team-creation "
        "transaction, so its pool figure predates match_pool -- and the post "
        "happens while the write lock is held")
