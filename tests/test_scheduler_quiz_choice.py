import random
from cogs.unified_scheduler_cog import choose_scheduled_quiz_poster


def test_returns_both_in_some_order():
    a, b = object(), object()
    order = choose_scheduled_quiz_poster(random.Random(0), a, b)
    assert set(order) == {a, b} and len(order) == 2
