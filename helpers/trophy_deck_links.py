"""Map trophy (3-0) finishers to their stored MagicProTools deck links and render
grouped summary lines. Pure functions so the daily/weekly cron summaries can be
unit-tested in isolation."""
from collections import Counter, OrderedDict


def session_trophy_links(matches, magicprotools_links):
    """`(drafter_id, link_or_None)` for drafters with exactly 3 wins in one session.

    matches: iterable of objects with a `winner_id` attribute.
    magicprotools_links: {drafter_id: {"link": url, ...}} or None.
    """
    win_counts = Counter(m.winner_id for m in matches if m.winner_id)
    links = magicprotools_links or {}
    return [
        (pid, (links.get(pid) or {}).get("link"))
        for pid, count in win_counts.items()
        if count == 3
    ]


def _deck_token(link, index=None):
    label = "deck" if index is None else f"deck {index}"
    return f"[{label}]({link})" if link else label


def _truncate_lines(lines, max_len):
    """Join lines with newlines within max_len; if they overflow, keep a prefix and
    append '…and K more' for the dropped remainder."""
    if not lines:
        return ""
    if len("\n".join(lines)) <= max_len:
        return "\n".join(lines)
    kept = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            trial = "\n".join(kept + [line, f"…and {len(lines) - i - 1} more"])
        else:
            trial = "\n".join(kept + [line])          # last line needs no marker
        if len(trial) <= max_len:
            kept.append(line)
        else:
            marker = f"…and {len(lines) - i} more"
            return "\n".join(kept + [marker]) if kept else marker
    return "\n".join(kept)


def render_grouped_trophy_decks(trophies, name_by_id, min_count=1, sort_by_count=False, max_len=1024):
    """Render grouped trophy deck lines.

    trophies: [(drafter_id, link_or_None), ...] across the window (order preserved).
    name_by_id: callable drafter_id -> display name.
    Groups by drafter_id; one line per drafter with >= min_count trophies:
      1 trophy : 'Name — [deck](l)'   (unlinked 'deck' when link is None)
      N trophies:'Name xN — [deck 1](l1), [deck 2](l2), ...'
    sort_by_count: order lines by trophy count descending when True.
    Returns lines joined by newlines, truncated to max_len with a trailing
    '…and K more' when needed; '' when nothing qualifies.
    """
    grouped = OrderedDict()
    for pid, link in trophies:
        grouped.setdefault(pid, []).append(link)

    items = [(pid, links) for pid, links in grouped.items() if len(links) >= min_count]
    if sort_by_count:
        items.sort(key=lambda kv: len(kv[1]), reverse=True)

    lines = []
    for pid, links in items:
        name = name_by_id(pid)
        if len(links) == 1:
            lines.append(f"{name} — {_deck_token(links[0])}")
        else:
            tokens = ", ".join(_deck_token(link, i + 1) for i, link in enumerate(links))
            lines.append(f"{name} x{len(links)} — {tokens}")

    return _truncate_lines(lines, max_len)
