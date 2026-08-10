"""Regression tests for ready-check auto-ready of synthetic test users.

Real Discord snowflakes exceeded TEST_USER_ID_START (9e17) for accounts
created after ~Oct 2021, so a bare `>=` check auto-readied real players in
production (e.g. user 1306388347043971156). Auto-ready must only apply to
the fake IDs minted by the TEST_MODE "Add Test Users" button.
"""
from views import TEST_USER_ID_START, is_synthetic_test_user

# A real player's snowflake (account created ~2024) that sits above 9e17.
REAL_NEW_ACCOUNT_ID = "1306388347043971156"


def test_real_new_account_is_not_synthetic_even_in_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    assert not is_synthetic_test_user(REAL_NEW_ACCOUNT_ID)


def test_synthetic_id_outside_test_mode_is_not_auto_readied(monkeypatch):
    monkeypatch.delenv("TEST_MODE", raising=False)
    assert not is_synthetic_test_user(str(TEST_USER_ID_START + 1))


def test_synthetic_id_in_test_mode_is_auto_readied(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    # The button mints TEST_USER_ID_START + i for small i; all must qualify.
    for i in range(1, 9):
        assert is_synthetic_test_user(str(TEST_USER_ID_START + i))


BOT_USER_ID = "1000000000000000001"   # the bot's own snowflake (arbitrary)


def test_bot_id_signup_is_test_signup_in_test_mode(monkeypatch):
    """The first 'Add Test Users' slot reuses the bot's own id; it must
    auto-ready like the synthetic users or a test ready check never completes."""
    monkeypatch.setenv("TEST_MODE", "true")
    from views import is_test_signup
    assert is_test_signup(BOT_USER_ID, BOT_USER_ID)


def test_bot_id_signup_not_test_signup_outside_test_mode(monkeypatch):
    monkeypatch.delenv("TEST_MODE", raising=False)
    from views import is_test_signup
    assert not is_test_signup(BOT_USER_ID, BOT_USER_ID)


def test_real_player_still_not_test_signup_in_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    from views import is_test_signup
    assert not is_test_signup(REAL_NEW_ACCOUNT_ID, BOT_USER_ID)


def test_synthetic_users_still_covered_by_is_test_signup(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    from views import is_test_signup
    assert is_test_signup(str(TEST_USER_ID_START + 1), BOT_USER_ID)
