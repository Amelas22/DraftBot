"""Match typed card names against a drafter's pool.

Pure helper: no database and no Discord. The "borrow only some cards" flow
hands over up to three typed names and gets back what they resolve to in the
pool, so the caller can ask about anything that is ambiguous instead of
lending the wrong card.
"""

import difflib
from dataclasses import dataclass, field
from typing import Any

# How close a fuzzy score must be to the best one to count as a tie. Two
# plausible spellings scoring this near each other mean the typed name cannot
# be resolved honestly, so it is reported as ambiguous rather than guessed.
FUZZY_TIE_MARGIN = 0.05

# Minimum similarity for a misspelling to count as a match at all.
FUZZY_CUTOFF = 0.8


@dataclass(frozen=True)
class NameMatch:
    # Pool entries as {"name": <pool name>, "qty": 1}, deduped, in typed order.
    # Quantity is always 1 here: borrowing takes one copy of each named card,
    # whatever the pool holds.
    found: list[dict[str, Any]] = field(default_factory=list)
    # Typed text that matched nothing in the pool.
    unknown: list[str] = field(default_factory=list)
    # (typed text, pool names it could mean), candidates sorted alphabetically.
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)


def _resolve(key: str, by_key: dict[str, str]) -> str | list[str] | None:
    """Resolve one casefolded typed name against the pool.

    Returns the pool name for a unique hit, the sorted candidate names when
    the typed text could mean several cards, or None when nothing is close.
    """
    exact = by_key.get(key)
    if exact is not None:
        return exact

    substring_hits = sorted({name for k, name in by_key.items() if key in k})
    if len(substring_hits) == 1:
        return substring_hits[0]
    if len(substring_hits) > 1:
        return substring_hits

    close = difflib.get_close_matches(key, list(by_key), n=len(by_key), cutoff=FUZZY_CUTOFF)
    if not close:
        return None
    scores = {k: difflib.SequenceMatcher(None, key, k).ratio() for k in close}
    best = max(scores.values())
    tied = sorted(by_key[k] for k, score in scores.items() if best - score <= FUZZY_TIE_MARGIN)
    return tied[0] if len(tied) == 1 else tied


def match_names(typed: list[str], pool: list[dict[str, Any]]) -> NameMatch:
    """Resolve each typed name to one pool card, or to ambiguous/unknown.

    Each typed name is tried in three steps, and the first step that yields
    exactly one hit wins: an exact case-insensitive name match, then a
    substring match, then a fuzzy match for misspellings. Several hits at a
    step make the name ambiguous; no hits at any step make it unknown.
    """
    by_key: dict[str, str] = {}
    for entry in pool:
        name = str(entry["name"])
        by_key.setdefault(name.casefold().strip(), name)

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    unknown: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []

    for raw in typed:
        text = raw.strip()
        if not text:
            continue
        resolved = _resolve(text.casefold(), by_key)
        if resolved is None:
            unknown.append(text)
        elif isinstance(resolved, list):
            ambiguous.append((text, resolved))
        elif resolved not in seen:
            seen.add(resolved)
            found.append({"name": resolved, "qty": 1})

    return NameMatch(found=found, unknown=unknown, ambiguous=ambiguous)
