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
5. **Deploy**: Migration runs automatically on production restart

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

# Service management (production)
sudo systemctl restart draftbot.service      # Restart with auto-migration
sudo journalctl -u draftbot.service -f       # View logs
```

### Testing
- **Always use `pipenv run` for all commands**
- Test locally with a copy of production data
- Use `./fetch_prod_db.sh` to get production database
- Always test migrations before deployment
- **CRITICAL**: Check `config.py` - `TEST_MODE_ENABLED` should be `True` for testing
- **NEVER commit with `TEST_MODE_ENABLED = True`** - always reset to `False` before committing

## Code Patterns and Conventions

### Configuration System
- Guild-specific configurations in `config.py`
- Special guild (`SPECIAL_GUILD_ID`) has enhanced features
- Configuration files stored in `/configs/` directory as JSON
- **Test Mode**: `TEST_MODE_ENABLED` flag in `config.py` controls test features
  - Set to `True` for local testing/development
  - **MUST be `False` for production commits**

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
6. **Pre-commit Check**: Ensure `TEST_MODE_ENABLED = False` in `config.py`
7. **Deploy**: Use service restart for automatic migration

### Code Quality
- **Always use `pipenv run` for Python commands**
- Follow existing patterns and conventions
- Use type hints where appropriate
- Add comprehensive error handling
- Include logging for debugging
- Write descriptive commit messages
- **Pre-commit checklist**:
  - [ ] `TEST_MODE_ENABLED = False` in `config.py`
  - [ ] All commands tested with `pipenv run`
  - [ ] Database migrations tested locally

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
- Database backups: Automatic timestamped backups before migrations

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
└── systemd/               # Service configuration
```

This context should help you understand the codebase structure, development patterns, and operational procedures for working with DraftBot effectively.