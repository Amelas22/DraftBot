# Design: MTGO house lending — deliver and recall real cards

**Date:** 2026-08-14
**Status:** Proposed

## Problem

`/lend` records a card loan between two players, but nothing physical happens: the
players hand the cards over themselves and the bot only writes the obligation
(`create_card_loan`, `services/debt_service.py:113`, storing `card_name` on
`DebtLedger`). There is no way for the **house** to lend a card out of the MTGO
vault and later reclaim it.

The MTGO serve can already do both halves of the physical work — it gives cards
(`POST /trade` with `give: [{name, qty, catId}]`) and reclaims an exact printing
(`POST /recall` with `{user, card, catId, qty}`). What blocks us is identity:

**`recall` requires `catId`, the exact printing, and DraftBot has no concept of a
printing.** `/lend` takes card names as free text ("e.g. 'Lightning Bolt'"), and
`catId` appears nowhere in the codebase. "Lightning Bolt" names dozens of
printings; the vault holds specific ones. Without the catId we cannot ask for the
copy we actually lent back.

## Goal

The house lends the cards in a `.dek` file to a player, tracks the obligation, and
can reclaim exactly those printings later.

A `.dek` is MTGO's own deck format and carries precisely what we lack:

```xml
<Cards CatID="12345" Quantity="4" Sideboard="false" Name="Lightning Bolt" />
```

`CatID` + `Quantity` + `Name` is exactly the tuple the serve's give and recall
endpoints consume. The serve anticipated this: `ItemDto.CatId` is already
documented as *"exact printing (from a .dek import)"*.

## Non-Goals

- Changing player-to-player `/lend`. It stays as-is: free-text names, no delivery.
- Deck validation, legality, or collection management.
- A web upload surface (see "Ingestion" below).
- Partial recall of a subset of a loan's copies. A loan is recalled whole.

## What already exists (do not rebuild)

| Piece | Location |
|---|---|
| Card-denominated obligations, double-entry | `models/debt_ledger.py` — `card_name` column |
| `create_card_loan(guild_id, lender_id, borrower_id, card_name, quantity, …)` | `services/debt_service.py:113` |
| Generic card give/deposit over the serve | `services/mtgo_tradebot_client.py` — `give()`, `deposit()` |
| Durable in-flight job record + startup resumer | `models/mtgo_job.py`, `mtgo_resolution_service.resume_pending_jobs` |
| Ambiguous-POST recovery (job adoption) | `mtgo_tradebot_client._call(mark_ambiguous=True)`, `_recover_lost_job` |

## Ingestion: Discord attachment

The `.dek` arrives as a **slash-command attachment**, not a web upload.

py-cord 2.6.1 supports `discord.Attachment` as an option type; the file is fetched
with `await attachment.read()`. There is no precedent in this repo yet, but it is a
standard supported option.

Rejected: a file-upload page on the serve's dashboard. The dashboard is
token-authed and bound to the operator's tailnet, so it is the wrong surface for
players; Discord already establishes who is asking (`ctx.author`), which a web
upload would have to re-establish; and it is a much smaller change.

Parsing belongs in **DraftBot**, because DraftBot is what needs the printing for
the loan record. `xml.etree.ElementTree` handles `.dek` with no new dependency.

## Data model

A new table, `card_loan`. **Not** a `cat_id` column on `DebtLedger`.

`DebtLedger` is a pure double-entry claim ledger — two rows per event, balance is
`SUM(amount)`. Delivery state (which MTGO job moved it, whether it is still out,
which printing) is not a claim and would pollute that. The codebase already draws
this line: `MtgoJob` exists alongside `WalletTx` for exactly this reason, and its
docstring explains why the durable job record is separate from the ledger.

**`card_loan` is to `DebtLedger` what `MtgoJob` is to `WalletTx`.**

| Column | Notes |
|---|---|
| `id` | pk |
| `guild_id` | |
| `borrower_id` | Discord id of the player holding the cards |
| `mtgo_user` | their MTGO account (the serve trades with this) |
| `card_name` | as it appears in the `.dek` |
| `cat_id` | **the exact printing** — the whole point |
| `quantity` | copies |
| `job_id` | the serve job that delivered it (and later, that reclaimed it) |
| `status` | `pending` → `outstanding` → `returned`, or `failed` |
| `created_at`, `resolved_at` | |

The lender is always the house, so it is not a column. If player-funded house
loans are ever wanted, add a `lender_id` defaulting to a synthetic holder, matching
the `prize:tourney:<id>` / `system:in-flight` pattern in `wallet_service`.

## Flow: lend

`/lend_deck player:<member> deck:<attachment> [commit:<bool>]`

A separate command from `/lend`, because the semantics differ (house delivery vs.
recording a player-to-player loan) and overloading one command with an optional
attachment that silently changes what it does would be worse.

1. Read and parse the attachment into `(cat_id, name, qty)` rows.
2. Reject early: empty file, unparseable XML, any row missing `CatID`, more than
   the cap (below).
3. Resolve the borrower's MTGO account (`models/mtgo_account.py`).
4. Write `card_loan` rows with `status='pending'`, and an `MtgoJob`-style durable
   record so a bot restart mid-trade can still resolve.
5. `POST /trade` with `give: [{name, qty, catId}, …]` and the borrower's MTGO user.
6. Poll to a terminal state, reusing `mtgo_resolution_service`'s existing polling,
   ambiguous-POST adoption, and startup resumer.
7. On `done`: call `create_card_loan(...)` per card so the obligation lands in
   `DebtLedger` (the claim), and set `card_loan.status='outstanding'`.
   On `failed`: `status='failed'`, **no claim is written** — nothing moved.

The claim is written only on `done`, mirroring how wallet boundary crossings are
booked only once the job reports done. This is the rule that the TradeBot's
false-failure bug violated on 2026-08-13, so it matters that a `failed` job leaves
no obligation behind.

## Flow: recall

`/recall_loan loan_id:<id>` (or a button on a loans panel).

1. Look up the `outstanding` `card_loan`.
2. `POST /recall` with `{user: mtgo_user, card: card_name, catId: cat_id, qty}`.
   The serve requires `catId` and 400s without it.
3. Poll as above.
4. On `done`: settle the `DebtLedger` claim via the existing settlement path
   (`source_type='settlement'`), and set `status='returned'`.

## Validation and failure modes

**Ownership is the serve's job, not ours.** The serve already fails closed when it
cannot offer a printing — `"cannot offer 'X' — not owned in any printing; aborting
the offer binder"` — and aborts before opening a trade. Duplicating an ownership
check here would need a full vault listing the serve does not expose, and would
race the vault anyway. Surface the serve's `detail` string to the user instead.

**Size cap.** Give and recall have very different costs:

- A *give* builds one offer binder containing every card, so cost is roughly flat
  in the number of cards.
- A *recall* is a receive, and the serve issues one wishlist request per item —
  measured at ~1.5s each plus polling. A 75-card recall is minutes.

So the cap exists for recall's sake. Proposal: **30 distinct printings per loan**,
rejected at parse time with a clear message. A deck larger than that should be
split into several loans, each recallable independently.

**Sideboard rows.** `.dek` marks them `Sideboard="true"`. Proposal: **include
them**, flattened into one list — the house is lending physical cards, and the
main/side split is a deck-construction concept that does not survive the trade.
Worth confirming; see Open questions.

## Migration, typing, tests

- Alembic revision adding `card_loan` (`pipenv run alembic revision --autogenerate`).
- New modules go into `pyrefly.toml` `project-includes` and must pass
  `pipenv run pyrefly check` with 0 errors, per CLAUDE.md.
- Tests alongside `tests/test_card_lending_service.py`: `.dek` parsing (including
  sideboard rows and a malformed file), claim written only on `done`, recall
  settling the claim, and cap enforcement.

## Open questions

1. **Is there an existing `2026-08-05-card-lending-design` spec?** It is cited by
   `tests/test_card_lending_service.py` but is not in the repo. If it exists it may
   already decide some of the above, and should win.
2. **Sideboard**: include (proposed) or exclude?
3. **Cap**: is 30 distinct printings right?
4. **Who may lend?** Admin-only, or any player with a role? `/lend` today is
   unrestricted because it only records; house lending moves real assets.
5. **Recall authority**: borrower-initiated return, house-initiated recall, or both?
