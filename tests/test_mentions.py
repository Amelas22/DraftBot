from helpers.mentions import SAFE_DEFAULT, pings


def test_safe_default_blocks_everyone_and_roles():
    d = SAFE_DEFAULT.to_dict()
    assert d["parse"] == ["users"] and "roles" not in d


def test_pings_names_exact_roles_only():
    d = pings(roles=["111", 222]).to_dict()
    assert sorted(d["roles"]) == [111, 222]
    assert "everyone" not in d["parse"] and "roles" not in d["parse"]


def test_pings_can_restrict_users():
    d = pings(roles=[1], users=["5"]).to_dict()
    assert d["users"] == [5] and "users" not in d["parse"]


def test_bot_is_built_with_safe_default():
    from bot import make_bot

    assert make_bot().allowed_mentions.to_dict() == SAFE_DEFAULT.to_dict()
