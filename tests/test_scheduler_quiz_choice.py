from cogs.unified_scheduler_cog import select_scheduled_poster


def test_pick_type_routes_to_pick_cog_only():
    pick, trophy = object(), object()
    assert select_scheduled_poster("pick", pick, trophy) is pick


def test_trophy_type_routes_to_trophy_cog_only():
    pick, trophy = object(), object()
    assert select_scheduled_poster("trophy", pick, trophy) is trophy


def test_missing_cog_for_type_returns_none():
    assert select_scheduled_poster("trophy", object(), None) is None
    assert select_scheduled_poster("pick", None, object()) is None


def test_unknown_type_returns_none():
    assert select_scheduled_poster("bogus", object(), object()) is None
