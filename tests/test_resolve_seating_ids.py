"""Unit tests for resolve_seating_ids — mapping a desired username order to
Draftmancer userIDs. The old name->id dict collapsed duplicate display names
(one userID seated twice, the other player omitted); this maps each occurrence
of a name to a DISTINCT userID (Codex review finding #2)."""

from helpers.seating import resolve_seating_ids


def _u(uid, name):
    return {"userID": uid, "userName": name}


def test_resolve_seating_ids_basic_order():
    users = [_u("a", "Alice"), _u("b", "Bob"), _u("bot", "DraftBot")]
    order, missing = resolve_seating_ids(users, ["Bob", "Alice"], "bot")
    assert order == ["b", "a"]
    assert missing == []


def test_resolve_seating_ids_duplicate_names_get_distinct_ids():
    # two players both named "Sam" must resolve to two DIFFERENT userIDs, both seated
    users = [_u("1", "Sam"), _u("2", "Sam"), _u("bot", "DraftBot")]
    order, missing = resolve_seating_ids(users, ["Sam", "Sam"], "bot")
    assert order == ["1", "2"]
    assert missing == []


def test_resolve_seating_ids_excludes_bot_and_reports_missing():
    users = [_u("bot", "DraftBot"), _u("a", "Alice")]
    order, missing = resolve_seating_ids(users, ["Alice", "Ghost"], "bot")
    assert order == ["a"]
    assert missing == ["Ghost"]


def test_resolve_seating_ids_more_names_than_users_of_that_name():
    # three "Sam" seats but only two Sam users → the third is missing, not a reused id
    users = [_u("1", "Sam"), _u("2", "Sam")]
    order, missing = resolve_seating_ids(users, ["Sam", "Sam", "Sam"], None)
    assert order == ["1", "2"]
    assert missing == ["Sam"]
