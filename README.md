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

Contribution
------------

Contributions to DraftBot are welcome! Please follow the project's contribution guidelines for submitting patches or features.

License
-------

DraftBot is released under the GNU General Public License v3.0 License. See the LICENSE file for more details.

* * * * *

This README provides an overview and guidance for using and contributing to the DraftBot project. For any further details or specific functionality, users and contributors should refer to the source code comments or contact the project maintainers.
