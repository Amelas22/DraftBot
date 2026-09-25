"""Whether a cube can be drafted from a given shelf.

A pure comparison of two {name: qty} maps, kept apart from the inventory tests
because it needs no database and no clock -- and because the module-level
asyncio mark over there would be applied to every one of these.

The rule is quantity-aware on purpose: drafting is without replacement, so a
cube running three Lightning Bolt needs three copies. Counting distinct names
would call a singleton library enough for a cube that triples half its list.
"""
import services.card_library_inventory as inv


def _cube(*pairs):
    return [{"name": n, "qty": q} for n, q in pairs]


def test_a_cube_is_supported_when_every_card_clears_its_quantity():
    """Drafting is without replacement, so a cube running three Bolts needs
    three copies on the shelf -- not one."""
    support = inv.cube_support(_cube(("Swamp", 3), ("Island", 1)),
                               {"Swamp": 3, "Island": 2})

    assert support.ok
    assert support.missing == []


def test_one_copy_short_is_not_supported():
    support = inv.cube_support(_cube(("Swamp", 3)), {"Swamp": 2})

    assert not support.ok
    assert support.missing == [{"name": "Swamp", "want": 3, "have": 2, "short": 1}]


def test_a_card_the_library_has_never_seen_reads_as_none_held():
    support = inv.cube_support(_cube(("Black Lotus", 1)), {"Swamp": 40})

    assert support.missing == [
        {"name": "Black Lotus", "want": 1, "have": 0, "short": 1}]


def test_the_shortfall_is_the_shopping_list_in_cube_order():
    """What a cube is short of IS what to tell a donor, so it is reported in
    the order the cube lists it rather than sorted by name or by severity."""
    support = inv.cube_support(
        _cube(("Zoo", 2), ("Ant", 1), ("Mox", 3)), {"Ant": 1, "Mox": 1})

    assert [m["name"] for m in support.missing] == ["Zoo", "Mox"]
    assert [m["short"] for m in support.missing] == [2, 2]


def test_surplus_copies_do_not_make_a_cube_more_supported():
    support = inv.cube_support(_cube(("Swamp", 1)), {"Swamp": 40})

    assert support.ok and support.missing == []


def test_an_empty_cube_is_vacuously_supported():
    """A cube that could not be read is None, not []; [] genuinely means a cube
    with no cards in it, and there is nothing the library lacks for it."""
    assert inv.cube_support([], {}).ok


def test_support_is_asked_of_whichever_shelf_you_mean():
    """The same comparison answers two questions, so it takes the map rather
    than fetching one: holdings for 'does the library own enough for this cube'
    -- the donor's question -- and available for 'can it be drafted today'."""
    cube = _cube(("Swamp", 2))

    assert inv.cube_support(cube, {"Swamp": 2}).ok, "owned outright"
    assert not inv.cube_support(cube, {"Swamp": 1}).ok, "half of it is out"
