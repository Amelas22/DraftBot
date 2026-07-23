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
                                "type": "Legendary Creature", "img": "http://img/minsc",
                                "arch": None, "lift": None, "wheel": None}  # English preferred
    assert seat["picks"][1]["name"] == "Arid Mesa"               # P2p1 pick included


def test_cube_family_from_key():
    mod = _load()
    assert mod.cube_family_from_key("team/PowerLSV-1784686408228-DBX.json") == "powerlsv"
    assert mod.cube_family_from_key("swiss/LSVCube-1-DBY.json") == "lsvcube"


def test_load_signal_map_missing_file_is_empty(tmp_path):
    mod = _load()
    assert mod.load_signal_map("nope", str(tmp_path)) == {}


def test_load_signal_map_reads_cards(tmp_path):
    import json
    (tmp_path / "strategy_x.json").write_text(json.dumps(
        {"cards": [{"name": "Reanimate", "arch": "UB", "arch_lift": 3.2, "wheel": 0.01}]}))
    mod = _load()
    assert mod.load_signal_map("x", str(tmp_path)) == {"Reanimate": {"arch": "UB", "lift": 3.2, "wheel": 0.01}}


def test_build_table_data_attaches_signal_fields():
    mod = _load()
    dd = _draft_data()  # existing helper (PDunny took "Minsc & Boo")
    sm = {"Minsc & Boo": {"arch": "RG", "lift": 2.1, "wheel": 0.2}}
    tbl = mod.build_table_data(dd, "me", sm)
    p0 = tbl["seats"][0]["picks"][0]
    assert (p0["arch"], p0["lift"], p0["wheel"]) == ("RG", 2.1, 0.2)
    # a card not in the map → nulls
    p1 = tbl["seats"][0]["picks"][1]
    assert (p1["arch"], p1["lift"], p1["wheel"]) == (None, None, None)


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


def _pass_draft():
    # pack-0 pass order me -> a -> b -> c (a fully-wheeled card seen by all, in order)
    def picks(seq_by_pick):
        return [{"packNum": 0, "pickNum": p, "booster": ["W"], "pick": [0]} for p in seq_by_pick]
    return {"users": {
        "me": {"userName": "Me", "picks": picks([0])},
        "ua": {"userName": "A", "picks": picks([1])},
        "ub": {"userName": "B", "picks": picks([2])},
        "uc": {"userName": "C", "picks": picks([3])},
    }, "carddata": {"W": {"name": "W", "colors": [], "rating": 1}}}


def test_build_seat_ring_from_pass_order():
    mod = _load()
    # shared physical card "W" seen by all four in pass order me,a,b,c
    dd = _pass_draft()
    for uid, seqpick in [("me", 0), ("ua", 1), ("ub", 2), ("uc", 3)]:
        dd["users"][uid]["picks"][0]["booster"] = ["W"]
    ring = mod.build_seat_ring(dd, "me")
    assert ring == ["Me", "A", "B", "C"]


def test_build_seat_ring_none_when_ambiguous():
    mod = _load()
    dd = {"users": {"me": {"userName": "Me", "picks": []},
                    "x": {"userName": "X", "picks": []}}, "carddata": {}}
    assert mod.build_seat_ring(dd, "me") is None
