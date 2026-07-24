"""Tests for stake display in team_creator._add_stake_info_to_embed.

Regression (Codex review finding): the bet field was built by RE-PARSING the
already-formatted stake strings ("{a} vs {b}: {amount} tix") with
line.split(': ') and parts[0].split(' vs '). A player whose display name
contains ': ' or ' vs ' mis-splits — wrong bold placement or IndexError.
The fix consumes the STRUCTURED (red, blue, amount) pairs directly.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def _run_add_stake_info(stake_pairs, total):
    """Call _add_stake_info_to_embed with get_stake_pairs mocked to return
    (stake_pairs, total). Returns the (lines, title) captured from
    add_links_to_embed_safely."""
    from services import team_creator

    embed = MagicMock()
    session = MagicMock()
    session.session_id = "s1"
    session.sign_ups = {"1": "x", "2": "y"}

    captured = {}

    def _capture(embed_arg, lines, title):
        captured["lines"] = lines
        captured["title"] = title

    with patch.object(team_creator, "get_stake_pairs",
                      AsyncMock(return_value=(stake_pairs, total))), \
         patch.object(team_creator, "add_links_to_embed_safely", _capture):
        # stake_info_by_player just needs to be truthy to pass the early return
        await team_creator._add_stake_info_to_embed(embed, session, {"1": 50})
    return captured


@pytest.mark.asyncio
async def test_add_stake_info_bolds_simple_names():
    captured = await _run_add_stake_info([("Alice", "Bob", 50)], 50)
    assert captured["lines"] == ["**Alice** vs **Bob**: 50 tix"]
    assert "Total: 50 tix" in captured["title"]


@pytest.mark.asyncio
async def test_add_stake_info_preserves_names_with_delimiters():
    # names containing ': ' and ' vs ' must NOT be mangled by delimiter splitting
    captured = await _run_add_stake_info([("Alice: the Great", "Bob vs Evil", 30)], 30)
    assert captured["lines"] == ["**Alice: the Great** vs **Bob vs Evil**: 30 tix"]
