---
name: pyrefly-typing
description: Conventions for typed Python in DraftBot — which files pyrefly checks, how to opt a new file in, and the preferred narrowing patterns for py-cord's awkward types (not_none, ui_button, as_messageable, cast). Use when adding or editing bot-code .py files, when pyrefly reports errors, or when running pyrefly from a git worktree.
---

# Pyrefly typing conventions


The project is migrating to typed Python gradually. `pyrefly.toml` runs at
`strict`, but only over the files listed in its `project-includes` — everything
else is unchecked for now. The list grows one module at a time so each cleanup
stays small and reviewable.

**When you create a new bot-code `.py` file (cogs, helpers, services, models,
views — not migrations, one-off scripts, or tests), add it to `project-includes`
in `pyrefly.toml` and make sure `pipenv run pyrefly check` still reports 0
errors.** New code should be born type-clean; that is what stops the untyped
surface from growing while the backlog is worked off. New typed helpers added
to an existing unlisted file don't force that whole file in — but if the file
is small, opting it in is the better call.

When touching an existing file that isn't listed yet, you may opt it in too, but
do it as its own commit — mixing a type cleanup into a behaviour change makes both
harder to review.

From a **git worktree**, `pipenv run` won't resolve this project's venv — bare
`pyrefly check` then falls back to system site-packages and reports phantom
missing-import errors. Use
`pyrefly check --python-interpreter-path "$(pipenv --py)"` (with `pipenv --py`
run from the main checkout), or set `VIRTUAL_ENV` to the project venv.

Scope caveat: `replace-imports-with-any` (see `pyrefly.toml`) means SQLAlchemy
model attributes type as `Any` — strictness covers the checked file's local
logic, not its model contracts (e.g. a nullable JSON column passed where a
`dict` is expected won't be caught).

Conventions for the awkward py-cord cases, in preference order — the goal is
that narrowing is declared ONCE at a boundary (or backed by a runtime check),
never re-asserted per use site:

- `not_none(x)` (in `helpers/utils.py`) asserts a value isn't `None`, raising at
  runtime if the assumption is wrong. Use it for things like
  `not_none(interaction.user).id`, where pycord's types allow `None` but the
  handler can only run when it's present. Use it sparingly — prefer a real
  `is not None` check when the value genuinely can be absent.
- `@ui_button(...)` (in `helpers/utils.py`) instead of `@discord.ui.button(...)`:
  py-cord swaps every decorated method attribute for its Button item at View
  init, so `ui_button` declares the attribute as the `Button` it actually is —
  `self.my_button.style = ...` then typechecks everywhere with no casts. The
  one static lie (the class attribute is the raw function until init, which
  nothing observes) lives inside the wrapper, documented.
- `as_messageable(x)` (in `helpers/utils.py`) narrows a `bot.get_channel`
  result to `Messageable` with a real isinstance check — a clear boundary
  error instead of an AttributeError deep in py-cord, and it tolerates
  threads/DMs where a `TextChannel` cast would not.
- `discord.ui.Button[Any]` — `Button` is generic over its parent view, which
  button callbacks don't depend on.
- `cast(...)` as a last resort, only for unions no isinstance can express, with
  a comment justifying each use. (The checked files currently contain none.)
- `# pyrefly: ignore [error-kind]` as a very last resort, on the line above the
  error.

