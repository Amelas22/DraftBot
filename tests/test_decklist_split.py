from services.draft_log_store import split_decklist, build_mtgo_deck_text

CARDDATA = {
    "m1": {"name": "Mana Drain"}, "m2": {"name": "Duress"}, "m3": {"name": "Duress"},
    "s1": {"name": "Negate"}, "s2": {"name": "Brainstorm"},
    "p1": {"name": "Ponder"},
}


def _dd(user):
    return {"users": {"u": user}, "carddata": CARDDATA}


def test_split_uses_decklist_when_built():
    dd = _dd({
        "cards": ["m1", "m2", "m3", "s1", "s2"],
        "decklist": {"main": ["m1", "m2", "m3"], "side": ["s1", "s2"], "lands": {"U": 6, "B": 5, "W": 0}},
    })
    split = split_decklist(dd, "u")
    assert split["main"] == ["m1", "m2", "m3"]
    assert split["side"] == ["s1", "s2"]
    assert split["basics"] == {"U": 6, "B": 5, "W": 0}


def test_split_falls_back_to_full_pool_when_unbuilt():
    dd = _dd({"cards": ["m1", "p1"]})  # no decklist
    split = split_decklist(dd, "u")
    assert split["main"] == ["m1", "p1"]
    assert split["basics"] == {}
    assert split["side"] == []


def test_split_falls_back_when_main_empty():
    dd = _dd({"cards": ["m1"], "decklist": {"main": [], "side": [], "lands": {}}})
    assert split_decklist(dd, "u")["main"] == ["m1"]


def test_mtgo_text_groups_main_maps_basics_and_separates_sideboard():
    split = {"main": ["m1", "m2", "m3"], "basics": {"U": 6, "B": 5, "W": 0}, "side": ["s1", "s2"]}
    text = build_mtgo_deck_text(split, CARDDATA)
    lines = text.split("\n")
    assert "1 Mana Drain" in lines
    assert "2 Duress" in lines               # grouped by name
    assert "6 Island" in lines and "5 Swamp" in lines
    assert "0 Plains" not in text            # zero basics skipped
    assert "" in lines                        # blank line separates sideboard
    blank = lines.index("")
    assert any("Negate" in l for l in lines[blank + 1:])   # sideboard after the blank line


def test_mtgo_text_no_sideboard_block_when_side_empty():
    split = {"main": ["m1"], "basics": {}, "side": []}
    text = build_mtgo_deck_text(split, CARDDATA)
    assert text == "1 Mana Drain"
    assert "\n\n" not in text
