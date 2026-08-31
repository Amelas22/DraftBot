# Draft lifecycle state: notes toward a cleaner design

Written while fixing the room-replacement decision that lost a draft log
(`eastern-paladin-84`, 2026-08-28). The first change is in; this records the
rest of what the investigation turned up, so the work that follows has a
starting point rather than being rediscovered.

## What happened

The trigger was a restart landing in a ~30-second window; the bug was what the
recovery did next.

```
16:43  teams formed; drafting begins in Draftmancer room DBYUMRE716
17:04:26  bot restarts. Socket dies, so Draftmancer reassigns session
          ownership to a human
17:04:39  manager A (rebuilt at startup) hits an ownership error on importCube.
          Stage is still 'teams', which the guard recognised -> creates
          pairings and hands over. Correct.
17:04:55  rooms created; stage becomes 'pairings'
17:04:56  manager A deregisters itself
17:05:49  a second manager is spawned for the same session
17:05:50  it repeats the same check. Stage is now 'pairings', which the guard
          did NOT recognise -> regenerates, abandoning the room. One second
          earlier it had seen all eight players still sitting in it.
~17:33    Draftmancer's ~28-minute retention expires. Log gone.
```

The sharp part: **manager A's correct recovery produced the state that made the
second manager destructive.** The same guard answered right at 17:04:39 and
wrong at 17:05:50, on the same session, because advancing to pairings moved the
stage from a value it knew to one it didn't.

## The invariant

Replacing a Draftmancer room is destructive and irreversible: the room is the
only copy of the log, and Draftmancer keeps it for about 28 minutes. So the
question is never "is it safe to keep the room" — keeping is always safe. It is
"do we have positive evidence the room is replaceable", and everything else
must preserve.

The original code inverted this. Every ambiguous answer routed to destruction:
a stage it didn't recognise, a session row it couldn't read, a users list that
hadn't arrived yet. `must_preserve_draft_room()` now expresses the invariant in
its name and defaults the other way.

## Three problems with `session_stage`

**1. It stops advancing.** Most fully played drafts finish at `pairings`, not
`completed` — 2472 rows against 758 at time of writing. `helpers/stale_drafts.py`
already works around this, deriving completion from victory-message columns
because the stage cannot be trusted for it.

**2. Ordering questions get asked with `==`.** Callers want "at or past X", but
a free-text column has no order, so they compare against one value and silently
miss the others. That is this bug's whole shape.

**3. Writers disagree about what a stage means.** `teams` is written when links
are published (`services/team_creator.py`); `pairings` is written when the room
workflow *starts*, not when it completes (`views.py`); `completed` on victory
(`utils.py`); `abandoned` by the abandon command. It is one column carrying
several different notions of progress.

## Sketch: split the model

Codex's review argued, convincingly, that an ordered enum alone is not enough
and that the three concerns should separate:

- **Local workflow state** — a monotonic, enforced Discord-side lifecycle.
- **Completion** — derived from evidence, since the stage already can't be
  trusted for it. `is_finished_draft` is a working proof of the approach.
- **Draftmancer room facts** — current room id, previous room ids, why it was
  replaced, capture status, ownership status. None of these are the same thing
  as workflow progress, and today they are inferred from a column that is.

A phase enum is useful later, but as an incident response it is over-scoped
unless it also separates those three.

## Deeper question, not yet answered

Should room replacement be decided from stored state at all? Once team creation
has published per-player links, humans may hold the old room regardless of what
the bot believes. The safer policy is to ask Draftmancer whether the room is
occupied or holds a finished draft, and preserve when it cannot be asked. The
current fix is conservative in the right direction, but it is still inferring
room safety from a Discord-side column.

## Defects found, not yet fixed

- **The capture reconciler cannot actually retry.** `reconcile_capture`'s
  docstring says it reconnects the owner socket, but
  `spawn_for_existing_session` returns an already-registered manager untouched —
  it does not ensure the socket is connected, pointed at the room the DB now
  names, or running a keepalive. On 2026-08-28 that produced 132 identical
  "no log yet" ticks over 2h45m. This is the difference between a bad minute and
  a lost log, and is the natural next change.
- **`keep_connection_alive` captures the websocket URL once**, before its loop,
  from `self.draft_id`. `regenerate_draft_session` mutates `self.draft_id`, so
  every reconnect after a regeneration targets the abandoned room.
- **`ACTIVE_MANAGERS` is not an ownership model.** It is a process-local dict
  that every constructor writes itself into, so construction *is* registration
  and can clobber another live manager. Cleanup sometimes checks identity before
  deleting and sometimes does not, and a manager whose initial connection fails
  is never deregistered. A registry tracking manager, task, room id, health and a
  generation token — with compare-and-swap on the expected `draft_id` before
  anything destructive — is the minimum rethink.
- **`StopRetryException` is swallowed.** `import_cube`'s broad `except Exception`
  catches it before the backoff decorator can see it, so a path that asked not to
  be retried is retried anyway.
- **`models/draft_session.py:213`** filters `session_stage != "COMPLETED"` in
  uppercase while every writer writes lowercase. It will not match NULL rows, and
  it fails to exclude the `completed` rows it is meant to exclude.
