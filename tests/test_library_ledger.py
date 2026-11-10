"""Pure rules of the card-library ledger: copies owned, peak holds, capacity."""

from datetime import datetime, timedelta

from services.library_ledger import (
    Span,
    copies_owned,
    counts_until,
    peak_held,
    verdict,
)

T0 = datetime(2026, 10, 20, 18, 0)
H = timedelta(hours=1)
CUBE = [{"name": "Bolt", "qty": 1}, {"name": "Twin", "qty": 2}]


def span(kind: str, start_h: float, end_h: float, released_h: float | None = None) -> Span:
    return Span(
        kind=kind,
        starts_at=T0 + start_h * H,
        ends_at=T0 + end_h * H,
        released_at=None if released_h is None else T0 + released_h * H,
    )


def test_copies_owned_is_set_by_the_scarcest_card():
    assert copies_owned({"Bolt": 4, "Twin": 6}, CUBE) == 3
    assert copies_owned({"Bolt": 4}, CUBE) == 0
    assert copies_owned({"Bolt": 4, "Twin": 6}, []) == 0


def test_peak_counts_overlap_only():
    spans = [span("match", 0, 3), span("match", 3, 6)]
    assert peak_held(spans, T0, T0 + 6 * H, T0) == 1
    spans.append(span("match", 1, 4))
    assert peak_held(spans, T0, T0 + 6 * H, T0) == 2


def test_a_released_hold_stops_counting_at_its_release():
    spans = [span("match", 0, 4, released_h=1)]
    assert peak_held(spans, T0 + 2 * H, T0 + 3 * H, T0 + 2 * H) == 0
    assert counts_until(spans[0], T0 + 2 * H) == T0 + 1 * H


def test_an_unreleased_hold_past_its_end_still_counts():
    spans = [span("sitting", 0, 4)]
    now = T0 + 6 * H
    assert peak_held(spans, T0 + 5 * H, T0 + 5 * H + timedelta(minutes=1), now) == 1


def test_server_may_use_any_copy():
    window = (T0, T0 + 4 * H, T0)
    three = [span("match", 0, 4) for _ in range(3)]
    assert verdict("server", 4, three, *window).ok
    four = three + [span("server", 0, 4)]
    assert not verdict("server", 4, four, *window).ok


def test_league_never_takes_the_server_copy():
    three = [span("match", 0, 4) for _ in range(3)]
    v = verdict("match", 4, three, T0, T0 + 4 * H, T0)
    assert not v.ok
    assert v.limit == 3


def test_a_server_hold_blocks_league_only_through_the_total():
    window = (T0, T0 + 4 * H, T0)
    league = [span("match", 0, 4), span("sitting", 0, 4)]
    two_server = league + [span("server", 0, 4), span("server", 0, 4)]
    assert not verdict("match", 4, two_server, *window).ok
    one_server = league + [span("server", 0, 4)]
    assert verdict("match", 4, one_server, *window).ok


def test_small_libraries_never_go_negative():
    window = (T0, T0 + 4 * H, T0)
    v = verdict("match", 1, [], *window)
    assert not v.ok
    assert v.limit == 0
    assert verdict("server", 1, [], *window).ok
    assert not verdict("match", 0, [], *window).ok
    assert not verdict("server", 0, [], *window).ok


def test_an_overdue_hold_counts_in_a_later_window():
    spans = [span("sitting", 0, 4)]
    now = T0 + 6 * H
    assert peak_held(spans, T0 + 7 * H, T0 + 9 * H, now) == 1
