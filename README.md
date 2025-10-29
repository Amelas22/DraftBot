DraftBot Documentation
======================

DraftBot is a Discord bot designed to automate and enhance the experience of organizing and conducting Magic: The Gathering (MTG) draft sessions on Discord, particularly focusing on team drafts with either random or premade teams. This bot utilizes Draftmancer, an online tool for simulating MTG drafts, to provide a seamless drafting organization experience.

Features
--------

-   Session Creation: Users can start new draft sessions with either random teams (`/startdraft`) or premade teams (`/premadedraft`).
-   Sign-up Management: Participants can sign up for a draft session, cancel their sign-up, or be removed by the organizer.
-   Team Management: For premade drafts, participants can join specific teams. The bot supports creating random teams for random drafts.
-   Ready Check: Initiates a check to ensure all participants are ready before proceeding.
-   Seating Order Generation: Randomly generates and displays a seating order for the draft.
-   Chat Channel Management: Automatically creates and manages Discord text channels for draft discussion, team communication, and posting pairings.
-   Pairings and Match Results: Posts match pairings and allows participants to report match results. Supports tracking wins and determining draft outcomes (victories or draws).

Commands
--------

-   `/startdraft`: Initiates a new draft session with random teams. Provides a link to a Draftmancer session and instructions for participants.
-   `/premadedraft`: Initiates a new draft session with premade teams. Allows participants to join either Team A or Team B.

How It Works
------------

1.  **Creating a Draft Session**: The bot supports two types of draft sessions—random and premade. Use the appropriate slash command to start a session. The bot will post an embed message with details and instructions.

2.  **Signing Up**: Participants can sign up for the draft by interacting with the bot's message. The bot tracks sign-ups and displays them in the embed message.

3.  **Forming Teams**: For random drafts, the bot will randomly assign signed-up participants to teams. For premade drafts, participants choose their teams.

4.  **Ready Check and Seating Order**: Once teams are formed, a ready check is initiated to ensure all participants are present and ready.

5.  **Drafting**: Participants join the Draftmancer session using the provided link and complete the draft according to the seating order.

6.  **Chat Channels**: The bot creates Discord text channels for draft discussion and team communication. Once the draft is completed, it posts pairings in the "draft-chat" channel.

7.  **Reporting Results**: Participants report match results through the bot. The bot updates the pairings message with the outcomes.

8.  **Determining the Outcome**: The bot calculates team wins to determine the draft outcome—victory for one team or a draw. Results are posted in both the "draft-chat" and "team-draft-results" channels.

9.  **Cleanup**: After the draft, chat channels are automatically deleted to tidy up the server.

Technical Details
-----------------

-   The bot uses Pycord for interaction handling and managing Discord components like buttons and embeds.
-   Session data is stored in memory and can be persisted to disk as JSON for recovery or archival purposes.
-   The bot handles asynchronous operations, such as creating channels and posting messages, to ensure a responsive user experience.
-   Uses SQLite database with SQLAlchemy ORM for persistent data storage.
-   Database schema management is handled through Alembic migrations.

Setup and Deployment
--------------------

1.  **Install Python 3.11** or newer.
2.  **Install Dependencies with Pipenv**:
    - Run `pipenv install` to set up all dependencies in a virtual environment. The `Pipfile` includes essential packages like `py-cord`, `aiobotocore`, `pandas`, `sqlalchemy`, `python-dotenv`, and more.
3.  **Set Up a Discord Bot Token**:
    - Create a `.env` file in the project root with the following content:
      ```
      BOT_TOKEN=your_discord_bot_token
      ```
4.  **Run the Bot**:
    - Activate the Pipenv environment with `pipenv shell`.
    - Start the bot using:
      ```bash
      python bot.py
      ```

Testing
-------

DraftBot includes a test suite to validate functionality. Tests are located in the `tests/` directory.

### Running Tests

**IMPORTANT:** Always use `python -m pytest` instead of `pytest` to ensure proper module imports:

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

### Why `python -m pytest`?

Using `python -m pytest` runs pytest as a module, which automatically adds the current directory to Python's path. This allows tests to import project modules (like `models`, `utils`, etc.) without additional configuration.

Using just `pytest` will result in import errors because the project root won't be in the Python path.

Contribution
------------

Contributions to DraftBot are welcome! Please follow the project's contribution guidelines for submitting patches or features.

License
-------

DraftBot is released under the GNU General Public License v3.0 License. See the LICENSE file for more details.

* * * * *

Database Management & Alembic
=============================

DraftBot uses SQLite with SQLAlchemy ORM for data persistence and Alembic for database migrations.

Database Models
---------------

Models are located in the `models/` directory:
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

All models must be imported in `models/__init__.py` to be recognized by Alembic.

Alembic Migrations
------------------

### Initial Setup

When fetching a copy of production database:

```bash
# 1. Copy production database to local
./fetch_prod_db.sh

# 2. Check if database already has alembic version table
sqlite3 drafts.db "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version';"

# 3a. If alembic_version table exists (production has migrations):
#     No additional setup needed - database is already migration-ready

# 3b. If alembic_version table does NOT exist (production never had alembic):
#     Stamp the database with the baseline revision (one-time only)
pipenv run alembic stamp a9c77df9cda3
```

### Creating New Migrations

When you modify models or add new tables:

```bash
# 1. Make your model changes in the appropriate files
# 2. Ensure models are imported in models/__init__.py
# 3. Generate migration
pipenv run alembic revision --autogenerate -m "description of changes"

# 4. Review the generated migration file in alembic/versions/
# 5. Test migration locally
pipenv run alembic upgrade head
```

### Common Migration Commands

```bash
# Check current database revision
pipenv run alembic current

# Show migration history
pipenv run alembic history

# Upgrade to latest
pipenv run alembic upgrade head

# Downgrade one revision
pipenv run alembic downgrade -1

# Check if database is up to date
pipenv run alembic check
```

### Production Deployment

1. **Backup production database**:
   ```bash
   cp drafts.db drafts.db.backup.$(date +%Y%m%d_%H%M%S)
   ```

2. **Deploy code changes**:
   ```bash
   git pull origin main
   pipenv install  # if dependencies changed
   ```

3. **Run migration** (if production already has alembic set up):
   ```bash
   pipenv run alembic upgrade head
   ```

4. **First-time migration setup** (if production never had alembic):
   ```bash
   # Stamp with baseline, then upgrade
   pipenv run alembic stamp a9c77df9cda3
   pipenv run alembic upgrade head
   ```

5. **Restart application**

### Best Practices

- **Always test locally first** with a copy of production data
- **Review generated migrations** - Alembic may detect unintended changes
- **Keep models aligned with database** - avoid nullable/constraint mismatches
- **Use descriptive migration messages**
- **Check if production has alembic before stamping**

### Development Workflow

#### Adding a New Model
1. Create model file in `models/new_model.py`
2. Define SQLAlchemy model class inheriting from `Base`
3. Import model in `models/__init__.py`
4. Add model to `__all__` list
5. Generate migration: `pipenv run alembic revision --autogenerate -m "add new_model table"`
6. Test migration locally
7. Deploy to production

Droplet Service Management
-------------------------

DraftBot runs as a systemd service on the DigitalOcean droplet, providing improved reliability and automated database management.

### Key Benefits
- Automatic restarts if the bot crashes
- Proper logging through systemd
- Starts automatically on server reboot
- **Automatic database migrations on restart**
- Environment variables managed through `.env` file

### Service Installation

To install the service on a new server, use the provided deployment script:

```bash
# Deploy service (run from repository root)
sudo ./deploy_service.sh
```

**The script automatically:**
- Copies service file to `/etc/systemd/system/`
- Sets proper permissions
- Reloads systemd daemon
- Enables the service
- Starts the service (if it was previously running)
- Provides status feedback and useful commands

**Manual installation** (if needed):
```bash
# Copy service file to systemd directory
sudo cp systemd/draftbot.service /etc/systemd/system/

# Enable the service
sudo systemctl daemon-reload
sudo systemctl enable draftbot.service

# Start the service
sudo systemctl start draftbot.service
```

### Common Commands

1. **Check Service Status**:
   ```bash
   sudo systemctl status draftbot.service
   ```

2. **Start/Stop/Restart the Bot**:
   ```bash
   sudo systemctl start draftbot.service
   sudo systemctl stop draftbot.service
   sudo systemctl restart draftbot.service
   ```

3. **View Service Logs**:
   ```bash
   sudo journalctl -u draftbot.service
   # For real-time log following:
   sudo journalctl -u draftbot.service -f
   ```

### Deployment Process

The deployment process is now fully automated:

```bash
# On production server
cd /root/DraftBot
git pull origin main
sudo systemctl restart draftbot.service
```

**The service automatically:**
1. Stops the bot
2. Applies any pending database migrations
3. Starts the bot with updated schema and code

### Safety Features

- **Migration failures prevent bot startup** - protects against broken deployments
- **Migrations run before bot starts** - ensures schema consistency
- **Automatic rollback available** - check logs and use `alembic downgrade` if needed

### Troubleshooting

**Service fails to start after update:**
1. Check service logs: `sudo journalctl -u draftbot.service`
2. Look for migration errors or Python import issues
3. If migration failed, fix the issue and restart: `sudo systemctl restart draftbot.service`

**Manual migration management:**
```bash
# Check current migration status
cd /root/DraftBot
pipenv run alembic current

# Manually run migrations
pipenv run alembic upgrade head

# Rollback if needed
pipenv run alembic downgrade -1
```

**Service file updates:**
If you modify the service file:
```bash
sudo systemctl daemon-reload
sudo systemctl restart draftbot.service
```

This README provides an overview and guidance for using and contributing to the DraftBot project. For any further details or specific functionality, users and contributors should refer to the source code comments or contact the project maintainers.
