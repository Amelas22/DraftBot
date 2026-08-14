# CLAUDE.md - Context for Agentic LLMs

This file provides comprehensive context for agentic LLMs (like Claude) working with the DraftBot repository.

## Project Overview

DraftBot is a sophisticated Discord bot designed to automate and enhance Magic: The Gathering (MTG) draft sessions on Discord. It focuses on team drafts with random or premade teams, integrating with Draftmancer for the actual drafting experience.

## Key Architecture Components

### Core Files
- `bot.py` - Main bot entry point, handles Discord client setup and event management
- `config.py` - Configuration management system with guild-specific settings
- `commands.py` - Core command implementations for draft management
- `draft.py` - Draft session logic and state management
- `utils.py` - Utility functions for cleanup, player management, and view registration

### Database Layer
- **ORM**: SQLAlchemy with SQLite database (`drafts.db`)
- **Migrations**: Alembic for database schema management
- **Models Directory**: `/models/` contains all database models
  - `draft_session.py` - Draft session data
  - `match.py` - Match results and history
  - `player.py` - Player statistics and limits
  - `team.py` - Team information and weekly limits
  - `stake.py` - Betting/stake information
  - `sign_up_history.py` - User join/leave tracking
  - `challenge.py` - Challenge management
  - `draft_logs.py` - Logging and backup data
  - `leaderboard_message.py` - Leaderboard tracking
  - `utility.py` - Utility models

### Discord Integration
- **Framework**: py-cord (Discord.py fork)
- **Views**: Interactive Discord components (buttons, modals, etc.)
- **Slash Commands**: Modern Discord command interface
- **Channel Management**: Automatic creation/deletion of draft channels

## Development Workflow

### Database Changes
1. **Modify Models**: Update SQLAlchemy models in `/models/` directory
2. **Import Models**: Ensure new models are imported in `models/__init__.py`
3. **Generate Migration**: `pipenv run alembic revision --autogenerate -m "description"`
4. **Test Migration**: `pipenv run alembic upgrade head`
5. **Back up production first if the migration destroys data** — production runs
   migrations unguarded, so a `DROP`/`DELETE` is irreversible there. See
   "Migrations run unguarded" under Production Environment.
6. **Deploy**: Migration runs automatically on production restart

### Common Commands
**IMPORTANT: Always use `pipenv run` for all Python commands to ensure proper virtual environment.**

```bash
# Environment setup
pipenv install
pipenv shell

# Database management
pipenv run alembic current                    # Check current migration
pipenv run alembic upgrade head               # Apply migrations
pipenv run alembic revision --autogenerate   # Generate new migration

# Running the bot locally
pipenv run python bot.py

# Type checking (must report 0 errors)
pipenv run pyrefly check

# Service management (production)
sudo systemctl restart draftbot.service      # Restart with auto-migration (UNGUARDED - back up first if it drops data)
sudo journalctl -u draftbot.service -f       # View logs
```

### Testing
- **Always use `pipenv run` for all commands**
- Test locally with a copy of production data
- Use `./fetch_prod_db.sh` to get production database
- Always test migrations before deployment
- **CRITICAL**: Set `TEST_MODE=true` in `.env` (or environment) to enable test features locally
- **NEVER set `TEST_MODE=true` in production** - production `.env` should not have this set

#### Running Tests
Tests are located in the `tests/` directory. **IMPORTANT:** Always use `python -m pytest` instead of `pytest`:

```bash
# Run all tests
pipenv run python -m pytest

# Run specific test file
pipenv run python -m pytest tests/test_seating_order.py

# Run with verbose output
pipenv run python -m pytest -v

# Run specific test
pipenv run python -m pytest tests/test_seating_order.py::TestSeatingOrder::test_generate_seating_order_premade
```

**Why `python -m pytest`?** Running pytest as a module (`python -m pytest`) automatically adds the current directory to Python's path, allowing tests to import project modules (`models`, `utils`, etc.) without additional configuration. Using just `pytest` will result in import errors.

#### Discord-in-the-loop testing (Claude-driven)

Claude can drive the test Discord server itself — run slash commands, click bot components, and screenshot the results — via the `discord-test` project skill (`.claude/skills/discord-test/SKILL.md`). Requires `TEST_GUILD_ID` in `.env` and a dedicated logged-in test account in the in-app browser; see the skill for the safety rails.

## Code Patterns and Conventions

### Configuration System
- Guild-specific configurations in `config.py`
- Special guild (`SPECIAL_GUILD_ID`) has enhanced features
- Configuration files stored in `/configs/` directory as JSON
- **Test Mode**: `is_test_mode()` function in `config.py` reads the `TEST_MODE` env var
  - Set `TEST_MODE=true` in `.env` for local testing/development
  - Production `.env` should not include `TEST_MODE`

### Error Handling
- Comprehensive logging with loguru
- Graceful error handling in Discord interactions
- Database transaction rollbacks on failures

### Discord Views and Interactions
- View classes for interactive components
- Persistent views that survive bot restarts
- Modal dialogs for user input

### State Management
- Draft sessions stored in database
- In-memory caching for active sessions
- Automatic cleanup of expired sessions

## Key Features

### Draft Management
- **Session Types**: Random teams, premade teams, Winston draft
- **Signup System**: Player registration with limits and validation
- **Team Formation**: Automatic random team assignment or manual selection
- **Ready Checks**: Ensure all players are present before starting
- **Seating Orders**: Random seating generation for drafts

### Match Management
- **Pairings**: Automatic pairing generation based on team assignments
- **Result Reporting**: Player-submitted match results
- **Leaderboards**: Performance tracking and statistics
- **Stakes/Betting**: Optional betting system with configurable multipliers

### Channel Management
- **Auto-creation**: Draft channels created automatically
- **Voice Channels**: Optional voice channel support
- **Category Organization**: Organized channel structure
- **Auto-cleanup**: Channels deleted after draft completion

### External Integrations
- **Draftmancer**: Web-based MTG draft simulator
- **CubeCobra**: Cube list management
- **Webhooks**: Integration with external services

## Development Guidelines

### Adding New Features
1. **Plan**: Use TodoWrite tool to plan implementation steps
2. **Models**: Create/modify database models first
3. **Commands**: Implement Discord slash commands
4. **Views**: Add interactive UI components
5. **Test**: Test with production data copy using `pipenv run`
6. **Pre-commit Check**: Ensure `TEST_MODE=true` is not in the production `.env`
7. **Deploy**: Use service restart for automatic migration

### Code Quality
- **Always use `pipenv run` for Python commands**
- Follow existing patterns and conventions
- Use type hints where appropriate
- Add comprehensive error handling
- Include logging for debugging
- Write descriptive commit messages
- **Pre-commit checklist**:
  - [ ] `TEST_MODE=true` is not set in production environment
  - [ ] All commands tested with `pipenv run`
  - [ ] Database migrations tested locally
  - [ ] `pipenv run pyrefly check` reports 0 errors

### Type Checking

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

### Security Considerations
- Never commit secrets or tokens
- Use environment variables for sensitive data
- Validate user inputs
- Implement proper permission checks
- Log security-relevant events

## Production Environment

### Deployment
- **Server**: DigitalOcean droplet
- **Service**: systemd service (`draftbot.service`)
- **Auto-restart**: Service restarts on failures
- **Auto-migration**: Database migrations run on service start
- **Logging**: Centralized logging through systemd

### Monitoring
- Service status: `sudo systemctl status draftbot.service`
- Real-time logs: `sudo journalctl -u draftbot.service -f`
- Database backups: **none are automatic.** See "Migrations run unguarded" below.

### Migrations run unguarded

`draftbot.service` runs `ExecStartPre=... alembic upgrade head` with no backup
step in front of it, and `alembic/env.py` has no hook of its own. Nothing copies
the production database before a migration touches it.

**Back up the production database yourself before deploying a migration that
destroys data** — any `DROP TABLE`, `DROP COLUMN`, or bulk `DELETE`/`UPDATE`.
Once the service restarts, the old rows are gone and there is nothing to roll
back to; a migration's `downgrade()` restores the schema, never the data.

Do not confuse this with `scripts/fetch_prod_db.sh`. Its timestamped
`drafts.db.backup.<stamp>` copy protects your **local** database from being
overwritten by the fetch. It runs on your machine, on demand, in the opposite
direction, and never touches the server.

### Configuration Management
- Environment variables in `.env` file
- Guild configurations in `/configs/` directory
- Feature flags for different guild types

## Troubleshooting

### Common Issues
1. **Migration Failures**: Check logs, fix migration, restart service
2. **Permission Errors**: Verify bot has necessary Discord permissions
3. **Database Locks**: Ensure no concurrent database access
4. **Memory Issues**: Monitor for memory leaks in long-running sessions

### Debugging Tools
- **Logs**: Comprehensive logging with loguru
- **Database**: SQLite browser for direct database inspection
- **Discord**: Bot developer portal for API debugging
- **Alembic**: Migration status and history commands

## File Structure Reference

```
DraftBot/
├── bot.py                 # Main bot entry point
├── config.py              # Configuration management
├── commands.py            # Core commands
├── draft.py               # Draft session logic
├── utils.py               # Utility functions
├── models/                # Database models
│   ├── __init__.py
│   ├── draft_session.py
│   ├── match.py
│   ├── player.py
│   └── ...
├── alembic/               # Database migrations
│   ├── env.py
│   └── versions/
├── cogs/                  # Discord command extensions
├── database/              # Database utilities
├── configs/               # Guild configurations
├── logs/                  # Application logs
├── tests/                 # Test suite
│   ├── test_seating_order.py
│   └── ...
└── systemd/               # Service configuration
```

This context should help you understand the codebase structure, development patterns, and operational procedures for working with DraftBot effectively.