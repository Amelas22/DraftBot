"""What the receipts and leaderboard channels say."""
from datetime import datetime, timedelta

from services.donation_day import Credit
from services.donation_day_discord import leaderboard_embed, receipt_messages

T0 = datetime(2026, 10, 16, 19, 0)
NAMES = {"1": "Ann", "2": "Ben", "3": "Cat", "4": "Dan"}


def _win(player, amount, at=T0, session="s1", draft="lightning-bolt-7"):
    return Credit(player, amount, at, "won", key=f"won:{session}:{player}",
                  method="", draft=draft)


def test_a_draft_win_is_one_receipt_naming_every_winner():
    credits = [_win("1", 100), _win("2", 50)]

    messages = receipt_messages(credits, NAMES, set(), set())

    assert len(messages) == 1
    keys, text = messages[0]
    assert sorted(keys) == ["won:s1:1", "won:s1:2"]
    assert "Ann" in text and "Ben" in text and "150 tix" in text
    assert "lightning-bolt-7" in text


def test_payments_are_receipts_and_cash_is_labelled():
    credits = [Credit("3", 50, T0, "paid", key="settle:7", method="cash"),
               Credit("4", 30, T0 + timedelta(minutes=1), "paid", key="settle:8", method="wallet"),
               Credit("1", 25, T0 + timedelta(minutes=2), "paid", key="gift:9", method="gift")]

    texts = [text for _, text in receipt_messages(credits, NAMES, set(), set())]

    assert "Cat" in texts[0] and "50 tix" in texts[0] and "(cash)" in texts[0]
    assert "Dan" in texts[1] and "(cash)" not in texts[1]
    assert "Ann" in texts[2] and "25 tix" in texts[2]


def test_announced_credits_are_not_announced_again():
    credits = [_win("1", 100), Credit("3", 50, T0, "paid", key="settle:7", method="wallet")]

    messages = receipt_messages(credits, NAMES, set(), {"won:s1:1", "settle:7"})

    assert messages == []


def test_a_draft_whose_winners_arrive_in_two_ticks_announces_only_the_new_one():
    """Keys are per winner, so a half-announced draft finishes rather than repeats."""
    messages = receipt_messages([_win("1", 100), _win("2", 50)], NAMES, set(), {"won:s1:1"})

    assert len(messages) == 1 and messages[0][0] == ["won:s1:2"]


def test_anonymous_donors_are_anonymous_in_receipts():
    credits = [Credit("3", 50, T0, "paid", key="settle:7", method="wallet")]

    [(_, text)] = receipt_messages(credits, NAMES, {"3"}, set())

    assert "Anonymous" in text and "Cat" not in text


def test_receipts_come_oldest_first():
    credits = [Credit("3", 50, T0 + timedelta(minutes=5), "paid", key="settle:2", method="wallet"),
               Credit("4", 30, T0, "paid", key="settle:1", method="wallet")]

    keys = [k for k, _ in receipt_messages(credits, NAMES, set(), set())]

    assert keys == [["settle:1"], ["settle:2"]]


def test_the_leaderboard_embed_shows_totals_and_ranked_rows():
    payload = {"name": "Donation Day", "goal": 500, "raised": 140, "pledged": 220,
               "updated_at": "2026-10-16T19:03",
               "rows": [{"name": "Cat", "won": 0, "paid": 90, "total": 90},
                        {"name": "Ann", "won": 50, "paid": 0, "total": 50}]}

    embed = leaderboard_embed(payload, "https://x/donations/dd/index.html")

    text = (embed.title or "") + (embed.description or "") + "".join(
        f.value for f in embed.fields)
    assert "Donation Day" in text and "$140" in text and "$500" in text and "$220" in text
    assert text.index("Cat") < text.index("Ann")
    assert "https://x/donations/dd/index.html" in text


def test_an_empty_leaderboard_says_so():
    payload = {"name": "DD", "goal": 100, "raised": 0, "pledged": 0,
               "updated_at": "2026-10-16T19:03", "rows": []}

    embed = leaderboard_embed(payload, None)

    assert "No donations yet" in "".join(f.value for f in embed.fields) + (embed.description or "")


def test_a_long_leaderboard_stays_inside_the_field_limit():
    rows = [{"name": "N" * 40, "won": 1000, "paid": 1000, "total": 2000} for _ in range(15)]
    payload = {"name": "DD", "goal": 100, "raised": 0, "pledged": 0,
               "updated_at": "2026-10-16T19:03", "rows": rows}

    embed = leaderboard_embed(payload, None)

    assert all(len(f.value) <= 1024 for f in embed.fields)
