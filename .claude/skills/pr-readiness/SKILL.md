---
name: pr-readiness
description: Use when a DraftBot change is finished and green and you are about to open a pull request, push a branch for review, mark a draft PR ready, or tell the user the work is ready — including when the suite passes, pyrefly is clean, and there is nothing obviously left to do.
---

# Before a DraftBot PR is ready

Green tests mean the code does what the tests say. They do not mean the change
is right, that it reads well, or that a player sees what you think they see.
Three passes catch what a green suite cannot, and each has caught real bugs in
this repo that the others missed.

**A PR is not ready until all three have run.** Passing tests is the entry
condition for this checklist, not a substitute for it.

## The three passes

| Pass | Catches | How |
|---|---|---|
| **Simplify** | duplication the change left behind, dead code it added, stale comments it invalidated | `/simplify` (4 parallel review agents over `git diff origin/main..HEAD`) |
| **Codex** | correctness, money-safety, idempotency, backward-compatibility for in-flight state | `/codex:rescue <what changed and what to look for>` |
| **E2E** | what a player actually sees | `superpowers:discord-test` — drive the real bot in the test guild |

Run simplify and Codex concurrently; they are read-only and independent. E2E
needs the bot running on the branch, so it goes last or in parallel with them.

## Why each one, in this repo

These are not hypothetical. Each pass has found something the others could not:

- **Simplify** found two embed titles built straight off a stored name — an
  unnamed draft announced *"None has won the match!"* — plus a fifth surviving
  copy of a helper the change existed to consolidate.
- **Codex** found two money-safety hazards in the prize-pool matcher and
  correctly identified both as pre-existing rather than regressions, which is
  the judgment that decides whether you fix or document them.
- **E2E** found a production regression that **three rounds of static auditing
  had walked straight past** — a message reading *"Added 6 test users. None:
  3/3"*. The string "Team A" appeared nowhere in it, so every grep and AST scan
  missed it. Only running the thing found it.

## Evidence document

Any change with a user-visible surface gets one: an Artifact showing each
changed surface, before → after, with the live screenshot beneath it.

Load `artifact-design` before writing it. Screenshots land in the repo root —
move them somewhere outside the repo and never commit them.

The document is for the reviewer. Do **not** link it from the PR body unless
the user asks: reviewers cannot open a private artifact, and a dead link reads
worse than a plain description of what was tested.

## When a pass finds something

Fix it and re-run the affected pass. A finding you decide not to act on gets
stated in the PR body with the reason — silently skipping one is how the next
reviewer inherits it.

Findings that turn out to be pre-existing rather than caused by the change are
worth saying so explicitly. It changes whether the fix belongs in this PR.

## Red flags — the PR is not ready

- "Tests pass, so it's ready"
- "It's a small change, the passes are overkill"
- "The change is behaviour-preserving so e2e can't tell me anything"
- "I already read the diff carefully"
- "I'll run them if review turns something up"
- "The user is waiting, I'll open it now and clean up after"

**All of these mean: run the three passes first.** The one time this session
they were skipped, the PR shipped with three stale docstrings, a subsumed test,
and a locator looser than the assertion behind it — all found the moment the
passes were finally run.

## Quick reference

```
1. suite green + pyrefly 0 errors        <- entry condition, not the finish line
2. /simplify                              } concurrently
   /codex:rescue <what changed>           }
3. superpowers:discord-test               <- bot on the branch, real guild
4. apply findings, re-run what they touched
5. evidence document (artifact-design) if a player-visible surface changed
6. full suite + pyrefly again
7. push, open PR, state any finding you chose not to act on
```

## Common mistakes

**Running the passes against the wrong range.** `git diff origin/main..HEAD`
after a `git fetch` — a stale `origin/main` silently reviews the wrong commits,
or reviews nothing at all.

**Trusting a guard you have not mutation-tested.** Write the check, break the
code it guards, watch it fail. A guard that asserts attribute names appear in a
function's source is satisfied by `e.name`, and passes with the fix deleted.

**Leaving screenshots in the repo.** They land in the working directory.
`bot_local.log` and `.playwright-mcp/` too.
