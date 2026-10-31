from services.card_name_match import match_names

POOL = [{"name": "Lightning Bolt", "qty": 1}, {"name": "Boltwave", "qty": 1},
        {"name": "Snapcaster Mage", "qty": 1}, {"name": "Island", "qty": 7}]


def test_exact_and_case_insensitive():
    assert match_names(["snapcaster mage"], POOL).found == [{"name": "Snapcaster Mage", "qty": 1}]

def test_a_unique_partial_name_matches():
    m = match_names(["snapcas"], POOL)
    assert m.found[0]["name"] == "Snapcaster Mage"
    assert m.unknown == [] and m.ambiguous == []

def test_a_misspelling_matches():
    m = match_names(["Snapcastor Mage"], POOL)
    assert m.found[0]["name"] == "Snapcaster Mage"
    assert m.unknown == [] and m.ambiguous == []

def test_a_partial_shared_by_two_cards_is_ambiguous_not_guessed():
    m = match_names(["bolt"], POOL)
    assert m.found == [] and m.ambiguous == [("bolt", ["Boltwave", "Lightning Bolt"])]

def test_a_misspelling_close_to_two_cards_is_ambiguous():
    # "serra angle" is no substring of either name, and scores 0.909 against
    # "Serra Angel" and 0.870 against "Serra Angels": both within 0.05 of the best.
    pool = [{"name": "Serra Angels", "qty": 1}, {"name": "Serra Angel", "qty": 1}]
    m = match_names(["serra angle"], pool)
    assert m.found == []
    assert m.unknown == []
    assert m.ambiguous == [("serra angle", ["Serra Angel", "Serra Angels"])]

def test_an_exact_name_wins_over_a_partial_rival():
    assert match_names(["boltwave"], POOL).found == [{"name": "Boltwave", "qty": 1}]

def test_not_in_the_pool_is_unknown():
    assert match_names(["Brainstorm"], POOL).unknown == ["Brainstorm"]

def test_an_empty_pool_reports_everything_unknown():
    m = match_names(["Lightning Bolt", "", "Island"], [])
    assert m.found == [] and m.ambiguous == []
    assert m.unknown == ["Lightning Bolt", "Island"]

def test_naming_a_card_twice_counts_once():
    assert match_names(["Island", "island"], POOL).found == [{"name": "Island", "qty": 1}]

def test_repeated_unknown_and_ambiguous_text_is_listed_once():
    m = match_names(["Brainstorm", "brainstorm", "bolt", "Bolt"], POOL)
    assert m.unknown == ["Brainstorm"]
    assert m.ambiguous == [("bolt", ["Boltwave", "Lightning Bolt"])]

def test_blank_fields_are_ignored():
    assert match_names(["", "  ", "Island"], POOL).found == [{"name": "Island", "qty": 1}]
