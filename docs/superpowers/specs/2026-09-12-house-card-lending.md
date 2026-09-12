# Design: house card lending (supersedes the 2026-08-14 spec's premise)

**Date:** 2026-09-12
**Status:** Implementing
**Supersedes:** `2026-08-14-mtgo-house-lending-design.md` (unmerged, branch `mtgo-card-lending`)

## What changed since the last design

The August design opens by naming its blocker:

> What blocks us is identity: `recall` requires `catId`, the exact printing, and DraftBot
> has no concept of a printing.

**That is no longer true.** The TradeBot now records the exact printing it observed crossing
the boundary, per user, and derives open positions from its own movement record:

| TradeBot surface | What it does |
|---|---|
| `POST /borrow` | gives cards **by name**; the ledger records which printings actually went |
| `POST /return` | receives back the **exact printings** lent — caller names only cards |
| `POST /withdraw` | gives back the **exact printings** deposited |
| `GET /positions?user=` | `held` (theirs, we keep) and `lent` (ours, they keep), per printing |

Three pillars of the old design existed only to solve that identity problem, and all three
are now unnecessary:

- **`cat_id` on a loan row.** DraftBot never needs a printing to get the right cards back.
- **`.dek` attachment ingestion.** It was the source of the catId. Lending by name works
  without it. (It remains a fine *future* feature for lending a chosen printing.)
- **The 30-printing cap.** It was sized for recall issuing one trade per printing. A
  settlement now spans several printings in ONE trade, so the cost model changed. A cap may
  still be wanted for wishlist-request time, but not at that number and not for that reason.

Two of its non-goals also fell out: partial return is supported (oldest position first), and
a single return can span printings.

## What stays true

The August design's data-model argument still holds and is adopted:

> `DebtLedger` is a pure double-entry claim ledger — delivery state is not a claim and would
> pollute it. `MtgoJob` exists alongside `WalletTx` for exactly this reason.

So the claim goes in `DebtLedger` (card rows already exist, via `card_name`) and the
delivery record goes in the job table. The difference from August: the job record does not
need a new table, because without `cat_id` a card job carries exactly what a tix job carries
plus a name.

## Data model

**`MtgoJob` gains a nullable `card_name`.** Null = tix, which is every existing row — the
same nullable-discriminator pattern `DebtLedger.card_name` already uses for the same reason
("multi-entity support: NULL = the entry is tix"). For a card job, `amount` is the quantity.
`kind` gains `borrow` and `return` beside `deposit` and `withdraw`.

Deliberately NOT a `card_loan` table: with `cat_id` gone, such a table would duplicate
`MtgoJob` column-for-column and add a second place for a delivery to be recorded.

**The house is a synthetic counterparty,** `house:mtgo`, following `wallet_service`'s
`system:in-flight` and `prize:tourney:<id>`. `is_system_account()` already returns True for
any non-numeric id, so every renderer that skips synthetic holders does the right thing with
no change. `_validated_card_args` only rejects self-loans and blank names, so it passes as-is.

## Flows

**Borrow** — `/borrow_card card:<name> quantity:<n>`

1. Resolve the borrower's MTGO account (`MtgoAccount.get_for_discord`).
2. `POST /borrow`; adopt the job on an ambiguous POST, exactly as tix does.
3. Record an `MtgoJob` with `kind='borrow'`, `card_name`, `amount=qty`.
4. Poll. On `done`, write the claim: `create_card_loan(house → borrower)`.
   On `failed`, write nothing — nothing moved.

**Return** — `/return_card card:<name> [quantity:<n>]`

1. `POST /return` with the card name. The TradeBot pins the printings itself.
2. Poll. On `done`, settle the card claim via `return_cards(borrower → house)`.

One card per command in v1. A card whose copies span several printings is still ONE command
and ONE job — the printing split lives entirely on the TradeBot side, which is the whole
point of the delegation.

**The claim is written only on `done`.** This mirrors wallet boundary crossings, and it is
the rule the TradeBot's false-failure bug violated on 2026-08-13: a `failed` job must leave
no obligation behind.

## Reconciliation

Tix has `reconcile()` asserting `vault tix == SUM(wallet rows)`. Cards now can too:
`GET /positions` lists every printing the bot says is out, and each should correspond to an
outstanding `DebtLedger` card claim. Not built here; recorded because `/positions` makes it
possible for the first time.

## Out of scope

`.dek` ingestion, lending a chosen printing, multi-card jobs, the reconciliation report, and
any change to player-to-player `/lend` (unchanged: free text, no delivery).
