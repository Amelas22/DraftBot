"""Cards that exist on paper but have never existed on MTGO.

The serve matches names exactly and refuses an ORDER containing one it does not
know, so the cost of a single unknown name is the whole trade -- forty-odd
innocent cards, as mtgo_names.py records for loan 19. A cube is where these get
in: CubeCobra lists what a cube plays, which is a paper question, and a cube
that drafts conspiracies lists them alongside three hundred perfectly ordinary
cards.

The list is STATIC on purpose, rather than asked of Scryfall at run time.
Availability here is a fact about sets that closed years ago and cannot change:
the Conspiracy sets never came to MTGO and never will. Asking the network
instead would buy nothing and cost a dependency, a cache, a rate limit, and an
answer for what to do when the lookup fails -- during a deposit, the worst
moment to have to decide.

Why these names and not a computed set: a per-card availability check is only
correct if it asks whether ANY printing is on MTGO. The obvious version --
read `games` off the printing a lookup hands back -- reports Goblin Welder,
Mind Stone, Mother of Runes and Wrath of God as paper-only, because the
printing returned is a recent paper-only product. That is the same per-PRINTING
trap helpers/cube_list.py documents for CubeCobra's `mtgo_id`. The test file
pins those four so the trap cannot be reintroduced as a "fix".

Extending it: the entry bar is a card with NO MTGO printing at all. Verify with
Scryfall's `game:mtgo !"<name>"` -- which searches every printing -- and add it
below. A card wrongly listed here is quietly undepositable; a card wrongly
missing costs a whole trade.
"""
from typing import Any

# Every card of the Conspiracy type. None has ever been on MTGO.
_CONSPIRACIES = (
    "Adriana's Valor", "Advantageous Proclamation", "Assemble the Rank and Vile",
    "Backup Plan", "Brago's Favor", "Double Stroke", "Echoing Boon",
    "Emissary's Ploy", "Hired Heist", "Hold the Perimeter", "Hymn of the Wilds",
    "Immediate Action", "Incendiary Dissent", "Iterative Analysis",
    "Muzzio's Preparations", "Natural Unity", "Power Play", "Secrets of Paradise",
    "Secret Summoning", "Sentinel Dispatch", "Sovereign's Realm",
    "Summoner's Bond", "Unexpected Potential", "Weight Advantage", "Worldknit",
)

# The Conspiracy sets' draft-matters cards -- ordinary card types that do
# something while the draft is happening. Printed only in those sets, so they
# are on MTGO no more than the conspiracies are.
_DRAFT_MATTERS = (
    "Aether Searcher", "Agent of Acquisitions", "Animus of Predation",
    "Canal Dredger", "Cogwork Grinder", "Cogwork Librarian", "Cogwork Spy",
    "Cogwork Tracker", "Deal Broker", "Illusionary Informant",
    "Leovold's Operative", "Lore Seeker", "Noble Banneret", "Paliano Vanguard",
    "Pyretic Hunter", "Regicide", "Spire Phantasm", "Whispergear Sneak",
)

UNTRADEABLE: "frozenset[str]" = frozenset(_CONSPIRACIES + _DRAFT_MATTERS)


def split_untradeable(
    cards: "list[dict[str, Any]]",
) -> "tuple[list[dict[str, Any]], list[str]]":
    """Separate a card list into what MTGO can move and what it cannot.

    Returns `(kept, dropped)`. `kept` holds the entries unchanged and in the
    cube's own order, so what is handed over still reads the way the cube does.
    `dropped` names each untradeable card ONCE, in the order first seen: it is
    shown to the depositor as a list of cards to fix, and a cube running two
    copies has one problem, not two.
    """
    kept: "list[dict[str, Any]]" = []
    dropped: "list[str]" = []
    for card in cards:
        name = card.get("name")
        if name in UNTRADEABLE:
            if name not in dropped:
                dropped.append(str(name))
            continue
        kept.append(card)
    return kept, dropped
