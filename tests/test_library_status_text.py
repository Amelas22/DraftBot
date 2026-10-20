"""What /library status says: copies, holds, and the next 24 hours."""
from datetime import datetime, timedelta

from services.library_ledger import Span
from services.library_status_text import status_lines

NOW = datetime(2026, 10, 19, 12, 0)


def _hold(kind, start, end, released=None):
    return Span(kind, start, end, released)


def test_status_lines_show_owned_held_free():
    live = _hold("server", NOW - timedelta(minutes=10), NOW + timedelta(hours=3))
    lines = status_lines("PowerLSV", 4, [live], NOW)
    assert "4 copies of PowerLSV" in lines[0]
    assert "1 held now" in lines[0]
    assert "3 free" in lines[0]
    assert "3 of 3 free" in lines[1]
    assert "Server copy: always kept" in lines[1]


def test_status_lines_timeline_shows_peaks():
    span = _hold("match", NOW + timedelta(hours=2), NOW + timedelta(hours=5))
    lines = status_lines("PowerLSV", 4, [span], NOW)
    hourly = lines[2:]
    assert len(hourly) == 24
    peaks = [int(line.rsplit(" ", 2)[-2]) for line in hourly]
    assert peaks[:2] == [0, 0]
    assert peaks[2:5] == [1, 1, 1]
    assert set(peaks[5:]) == {0}


def test_status_fits_discord():
    spans = [_hold("match", NOW + timedelta(hours=1), NOW + timedelta(hours=9))
             for _ in range(300)]
    lines = status_lines("PowerLSV", 400, spans, NOW)
    assert len("\n".join(lines)) <= 2000
