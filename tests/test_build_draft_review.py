import importlib.util, os

def _load():
    path = os.path.join(os.getcwd(), "scripts", "build_draft_review.py")
    spec = importlib.util.spec_from_file_location("build_draft_review", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def _draft_data():
    return {
        "users": {
            "me": {"userName": "aber", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c1"], "pick": [0]}]},
            "o1": {"userName": "PDunny", "picks": [
                {"packNum": 0, "pickNum": 0, "booster": ["c2", "c3"], "pick": [0]},
                {"packNum": 1, "pickNum": 0, "booster": ["c3"], "pick": [0]}]},
        },
        "carddata": {
            # Draftmancer keys image_uris by LANGUAGE (en, zhs, …), not size.
            "c1": {"name": "Ragavan", "colors": ["R"], "rating": 5, "cmc": 1, "type": "Creature",
                   "image_uris": {"en": "http://img/rag"}},
            "c2": {"name": "Minsc & Boo", "colors": ["R", "G"], "rating": 2.97, "cmc": 5,
                   "type": "Legendary Creature", "image_uris": {"zhs": "http://img/minsc-zh",
                                                                "en": "http://img/minsc"}},
            "c3": {"name": "Arid Mesa", "colors": [], "rating": 2.43, "cmc": 0, "type": "Land",
                   "image_uris": {"normal": "http://img/mesa"}},   # Scryfall-shaped fallback
        },
    }


def test_build_table_data_excludes_viewer_and_shapes_seats():
    mod = _load()
    tbl = mod.build_table_data(_draft_data(), "me")
    assert [s["name"] for s in tbl["seats"]] == ["PDunny"]        # viewer excluded
    seat = tbl["seats"][0]
    assert seat["picks"][0] == {"pack": 0, "pick": 0, "name": "Minsc & Boo",
                                "colors": ["R", "G"], "rating": 2.97, "cmc": 5,
                                "type": "Legendary Creature", "img": "http://img/minsc"}  # English preferred
    assert seat["picks"][1]["name"] == "Arid Mesa"               # P2p1 pick included


def test_assert_js_parses_catches_top_level_collision():
    import pytest
    mod = _load()
    mod.assert_js_parses("const X = 1;\nfunction f(){}\n")   # valid: no raise
    with pytest.raises(SystemExit):
        mod.assert_js_parses("const COLORS = 1;\nconst COLORS = 2;\n")   # redeclaration


def test_build_table_data_image_from_language_or_scryfall_shape():
    mod = _load()
    # language-keyed (en) resolves; Scryfall-shaped {normal} also resolves as fallback
    assert mod._card_image({"image_uris": {"zhs": "z", "en": "e"}}) == "e"
    assert mod._card_image({"image_uris": {"normal": "n"}}) == "n"
    assert mod._card_image({"image_uris": {"ja": "j"}}) == "j"    # any language when no en
    assert mod._card_image({}) == ""
