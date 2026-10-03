import pytest
from services.tournament_formatter import create_bracket_embed
from services.tournament_service import BracketRow


def _row(mid, stage, a, b, a_from=None, b_from=None, a_wins=None, b_wins=None, thread=None):
    return BracketRow(mid, stage, a, b, a_from, b_from, a_wins, b_wins, thread, False)


def _text(embed):
    return "\n".join(f.value for f in embed.fields)


def test_waiting_slot_names_its_feeder():
    embed = create_bracket_embed("Lotus", [_row(142, "Quarterfinal", (1, "🔥"), None, b_from=141)])
    assert "winner of #141" in _text(embed)


def test_decided_match_bolds_the_winner_with_the_score():
    embed = create_bracket_embed("Lotus", [_row(143, "Quarterfinal", (4, "CFB"), (5, "The initiative"),
                                               a_wins=5, b_wins=3)])
    assert "**(4) CFB** 5–3 (5) The initiative" in _text(embed)


def test_live_match_links_its_room():
    embed = create_bracket_embed("Lotus", [_row(144, "Quarterfinal", (2, "A"), (7, "B"), thread="900")])
    assert "<#900>" in _text(embed)


def test_bracket_embed_keeps_emoji_and_ampersand_names_intact():
    rows = [_row(141, "Play-in", (8, "gypsy caravan"), (9, "18 lands")),
            _row(144, "Quarterfinal", (2, "The Dog, The Turkey, & The Yak"), (1, "🔥"))]
    text = _text(create_bracket_embed("Lotus", rows))
    assert "The Dog, The Turkey, & The Yak" in text and "🔥" in text
    prefixes = [line.split("`")[1] for line in text.splitlines() if line.startswith("`")]
    assert len({len(p) for p in prefixes}) == 1        # id+stage column is one width


@pytest.mark.asyncio
async def test_update_bracket_message_edits_in_place(match_control_db):
    from unittest.mock import AsyncMock, MagicMock, patch
    from services import tournament_formatter as tf
    from tournament_fixtures import _swiss_done
    from services.tournament_service import start_playoff
    from test_bracket_rooms import _fake_db_session
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=4, teams=4)
        await start_playoff(session, t.id)
        t.bracket_channel_id, t.bracket_message_id = "55", "66"
        await session.commit()
        tid = t.id
    message = MagicMock(); message.edit = AsyncMock()
    channel = MagicMock(); channel.get_partial_message.return_value = message
    bot = MagicMock(); bot.get_channel.return_value = channel
    with patch.object(tf, "db_session", _fake_db_session(match_control_db)):
        await tf.update_bracket_message(bot, tid)
    assert "Semifinal" in _text(message.edit.call_args.kwargs["embed"])
