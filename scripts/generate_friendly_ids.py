#!/usr/bin/env python3
"""
One-time generator: builds a large list of dash-separated "friendly ids"
from Magic: The Gathering card names (e.g. "Lightning Bolt" ->
"lightning-bolt") and injects it into an existing Python file, for use
anywhere a short, human-readable, unique-ish identifier is nicer than a
random string.

Card data is sourced from Scryfall's "Oracle Cards" bulk export (one entry
per unique card, deduped across printings). The bulk file is looked up via
the bulk-data API rather than hardcoded, per Scryfall's own guidance, and
downloaded fresh on every run to a file under the system temp dir (just for
inspection -- it isn't reused on later runs).

Rather than overwriting the whole target file, this looks for a marker
comment ("### AUTO GENERATED FRIENDLY ID LIST BELOW HERE") in it (see
helpers/friendly_id.py) and replaces everything after that marker,
leaving everything above it -- imports, docstring, helper functions --
alone.

Run this once to (re)generate helpers/friendly_id.py's list; there's no
need to run it again unless you want to refresh against Scryfall's current
card pool.

Usage:
    pipenv run python scripts/generate_friendly_ids.py
    pipenv run python scripts/generate_friendly_ids.py helpers/friendly_id.py --max-length 15
"""
import argparse
import gzip
import json
import re
import sys
import tempfile
import unicodedata
import urllib.request
from pathlib import Path

BULK_DATA_INDEX_URL = "https://api.scryfall.com/bulk-data"
BULK_DATA_TYPE = "oracle_cards"
USER_AGENT = "DraftBot/1.0 (friendly-id generation script)"
LIST_VAR_NAME = "friendly_ids"
MARKER = "### AUTO GENERATED FRIENDLY ID LIST BELOW HERE\n### DO NOT EDIT\n"

SCRIPT_DIR = Path(__file__).parent
DEFAULT_TARGET_FILE = SCRIPT_DIR.parent / "helpers" / "friendly_id.py"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url))


def download_oracle_cards() -> list:
    """Fetch the current oracle-cards bulk data from Scryfall and stash a
    copy in an auto-named system temp file purely so it can be inspected --
    it's downloaded fresh every run, never reused."""
    print("Looking up the current oracle-cards bulk data URI...")
    bulk_data_index = fetch_json(BULK_DATA_INDEX_URL)
    entry = next(
        (item for item in bulk_data_index["data"] if item["type"] == BULK_DATA_TYPE),
        None,
    )
    if entry is None:
        sys.exit(f"Could not find a '{BULK_DATA_TYPE}' entry in the bulk-data index")

    # Scryfall serves this as gzipped JSON-Lines (one card object per line).
    download_uri = entry.get("jsonl_download_uri") or entry["download_uri"]
    size_bytes = entry.get("compressed_size") or entry.get("size") or 0
    print(f"Downloading {entry['name']} ({size_bytes / 1_000_000:.1f} MB)...")
    raw = fetch_bytes(download_uri)
    if download_uri.endswith(".gz"):
        raw = gzip.decompress(raw)

    if download_uri.endswith((".jsonl", ".jsonl.gz")):
        cards = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    else:
        cards = json.loads(raw)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="scryfall_oracle_cards_", delete=False
    ) as f:
        json.dump(cards, f)
        download_path = f.name
    print(f"Saved {len(cards)} cards to {download_path}")
    return cards


def slugify(card_name: str) -> str:
    """'Lightning Bolt' -> 'lightning-bolt'. For split/adventure/transform
    cards (name is 'Front // Back'), only the front face is used."""
    front_face = card_name.split(" // ")[0]
    ascii_name = unicodedata.normalize("NFKD", front_face).encode("ascii", "ignore").decode("ascii")
    # Fold possessives into the word they modify ("Zuko's" -> "zukos")
    # instead of leaving a stray one-letter "s" token behind.
    ascii_name = ascii_name.replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug


def is_real_card(card: dict) -> bool:
    """Filter out joke sets (Un-sets etc.) and digital-only cards (Alchemy
    rebalances, Arena-only cards), which aren't "real" paper Magic cards."""
    return card.get("set_type") != "funny" and not card.get("digital", False)


MIN_TOKEN_LENGTH = 3


def has_only_valid_tokens(slug: str) -> bool:
    """Reject slugs with a dash-separated token shorter than MIN_TOKEN_LENGTH
    (e.g. "a-i-m-bot", "2-mace") -- these read as noise, not words."""
    return all(len(token) >= MIN_TOKEN_LENGTH for token in slug.split("-"))


def build_friendly_ids(cards: list, min_length: int, max_length: int) -> list:
    seen = set()
    ids = []
    for card in cards:
        name = card.get("name")
        if not name or not is_real_card(card):
            continue
        slug = slugify(name)
        if not (min_length <= len(slug) <= max_length):
            continue
        if not has_only_valid_tokens(slug):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        ids.append(slug)
    ids.sort()
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "target_file",
        type=Path,
        nargs="?",
        default=DEFAULT_TARGET_FILE,
        help=f"Python file containing the AUTO GENERATED marker comment to inject the list after (default: {DEFAULT_TARGET_FILE})",
    )
    parser.add_argument("--min-length", type=int, default=3, help="Minimum slug length (default: 3)")
    parser.add_argument("--max-length", type=int, default=15, help="Maximum slug length (default: 15)")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of ids written (default: no cap)")
    args = parser.parse_args()

    cards = download_oracle_cards()
    ids = build_friendly_ids(cards, args.min_length, args.max_length)

    if args.limit is not None:
        ids = ids[: args.limit]

    inject_friendly_ids(args.target_file, ids)

    print(f"Wrote {len(ids)} friendly ids into {args.target_file}")
    print("Examples:", ", ".join(ids[:10]))


def render_list_literal(ids: list) -> str:
    lines = [f"{LIST_VAR_NAME} = ["]
    lines.extend(f'    "{card_id}",' for card_id in ids)
    lines.append("]")
    return "\n".join(lines)


def inject_friendly_ids(target_file: Path, ids: list) -> None:
    """Truncate `target_file` at the MARKER comment and replace everything
    after it with a freshly rendered friendly_ids list, leaving everything
    above the marker (imports, docstring, helper functions) untouched.
    Idempotent: rerunning always cuts at the same marker."""
    content = target_file.read_text()
    if MARKER not in content:
        sys.exit(f"Could not find the marker comment in {target_file}:\n{MARKER}")

    before_marker = content.split(MARKER, 1)[0]
    new_content = before_marker + MARKER + render_list_literal(ids) + "\n"
    target_file.write_text(new_content)


if __name__ == "__main__":
    main()
