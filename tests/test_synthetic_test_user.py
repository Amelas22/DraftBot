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
