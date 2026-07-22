from helpers.trophy_deck_links import session_trophy_links, render_grouped_trophy_decks


class _M:
    def __init__(self, winner_id):
        self.winner_id = winner_id


def _matches(winner_ids):
    return [_M(w) for w in winner_ids]


def _name(pid):
    return {"a": "Alice", "b": "Bob"}.get(pid, pid)


def test_session_trophy_links_only_three_win_ids():
    matches = _matches(["a", "a", "a", "b", "b", None])   # a=3 wins, b=2, one no-winner
    links = {"a": {"link": "http://x/a"}, "b": {"link": "http://x/b"}}
    assert session_trophy_links(matches, links) == [("a", "http://x/a")]


def test_session_trophy_links_missing_link_is_none():
    matches = _matches(["a", "a", "a"])
    assert session_trophy_links(matches, {}) == [("a", None)]
    assert session_trophy_links(matches, None) == [("a", None)]


def test_render_single_trophy():
    assert render_grouped_trophy_decks([("a", "L1")], _name) == "Alice — [deck](L1)"


def test_render_single_trophy_missing_link():
    assert render_grouped_trophy_decks([("a", None)], _name) == "Alice — deck"


def test_render_multiple_trophies_grouped():
    out = render_grouped_trophy_decks([("a", "L1"), ("a", "L2"), ("b", "L3")], _name)
    assert out == "Alice x2 — [deck 1](L1), [deck 2](L2)\nBob — [deck](L3)"


def test_render_min_count_filters_singletons():
    out = render_grouped_trophy_decks([("a", "L1"), ("a", "L2"), ("b", "L3")], _name, min_count=2)
    assert out == "Alice x2 — [deck 1](L1), [deck 2](L2)"


def test_render_missing_link_unlinked_token_keeps_count():
    out = render_grouped_trophy_decks([("a", "L1"), ("a", None)], _name)
    assert out == "Alice x2 — [deck 1](L1), deck 2"


def test_render_sort_by_count_desc():
    trophies = [("b", "L3"), ("a", "L1"), ("a", "L2")]   # b (1) appears before a (2)
    out = render_grouped_trophy_decks(trophies, _name, sort_by_count=True)
    assert out.splitlines()[0].startswith("Alice x2")


def test_render_empty():
    assert render_grouped_trophy_decks([], _name) == ""


def test_render_truncates_with_more_marker():
    url = "http://magicprotools.com/draft/show?id=" + "X" * 30
    trophies = [(str(i), url) for i in range(50)]
    out = render_grouped_trophy_decks(trophies, lambda p: f"Player{p}", max_len=200)
    assert len(out) <= 200
    assert out.rstrip().endswith("more")
