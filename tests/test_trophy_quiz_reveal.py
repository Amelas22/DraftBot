from services.trophy_quiz_service import apply_reveal_cost, REVEAL_COST, score_submission
from quiz_views_module.trophy_quiz_views import build_reveal_lines


def test_reveal_cost_constant():
    assert REVEAL_COST == 2


def test_apply_reveal_cost_floors_at_zero():
    assert apply_reveal_cost(10, False) == 10
    assert apply_reveal_cost(10, True) == 8
    assert apply_reveal_cost(4, True) == 2
    assert apply_reveal_cost(1, True) == 0
    assert apply_reveal_cost(0, True) == 0


def test_reveal_lines_unchanged_when_not_revealed():
    decks = [{"slot": "A", "drafter_id": "u1", "wins": 3},
             {"slot": "B", "drafter_id": "u2", "wins": 0}]
    result = score_submission([3, 0], [3, 0])
    assert build_reveal_lines(decks, [3, 0], result) == build_reveal_lines(decks, [3, 0], result, revealed=False)
    text = "\n".join(build_reveal_lines(decks, [3, 0], result, revealed=False))
    assert "Revealed" not in text


def test_reveal_lines_show_penalty_and_final_when_revealed():
    decks = [{"slot": "A", "drafter_id": "u1", "wins": 3},
             {"slot": "B", "drafter_id": "u2", "wins": 0}]
    result = score_submission([3, 0], [3, 0])   # base total 10
    lines = build_reveal_lines(decks, [3, 0], result, revealed=True)
    text = "\n".join(lines)
    assert "Revealed" in text and "-2" in text.replace("−", "-")
    assert "8" in text                            # penalized final
