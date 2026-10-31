from services.card_name_match import match_names

POOL = [{"name": "Lightning Bolt", "qty": 1}, {"name": "Boltwave", "qty": 1},
        {"name": "Snapcaster Mage", "qty": 1}, {"name": "Island", "qty": 7}]


def test_exact_and_case_insensitive():
    assert match_names(["snapcaster mage"], POOL).found == [{"name": "Snapcaster Mage", "qty": 1}]

def test_a_unique_partial_name_matches():
    assert match_names(["snapcas"], POOL).found[0]["name"] == "Snapcaster Mage"

def test_a_misspelling_matches():
    assert match_names(["Snapcastor Mage"], POOL).found[0]["name"] == "Snapcaster Mage"

def test_a_partial_shared_by_two_cards_is_ambiguous_not_guessed():
    m = match_names(["bolt"], POOL)
    assert m.found == [] and m.ambiguous == [("bolt", ["Boltwave", "Lightning Bolt"])]

def test_an_exact_name_wins_over_a_partial_rival():
    assert match_names(["boltwave"], POOL).found == [{"name": "Boltwave", "qty": 1}]

def test_not_in_the_pool_is_unknown():
    assert match_names(["Brainstorm"], POOL).unknown == ["Brainstorm"]

def test_naming_a_card_twice_counts_once():
    assert match_names(["Island", "island"], POOL).found == [{"name": "Island", "qty": 1}]

def test_blank_fields_are_ignored():
    assert match_names(["", "  ", "Island"], POOL).found == [{"name": "Island", "qty": 1}]
