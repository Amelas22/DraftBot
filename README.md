README for Magic: The Gathering Team Draft Bot
==============================================

Overview
--------

The Magic: The Gathering Team Draft Bot is a Discord application designed to facilitate and manage team draft events within the Discord platform. It automates several aspects of the drafting process, including participant sign-ups, team randomization, channel creation for team communication, and match pairings announcements. This tool aims to streamline the organization of draft events, making it easier for both organizers and participants to enjoy the draft. 

Features
--------

-   Automated Sign-Ups: Users can sign up for a draft event directly within Discord, using interactive buttons provided by the bot.
-   Team Randomization: Facilitates the creation of balanced teams by randomly assigning signed-up users to teams after the sign-up period ends.
-   Dedicated Draft Channels: Automatically generates Discord channels for each team, as well as a general channel for draft discussions and announcements.
-   Match Pairings Announcements: Clearly posts who is facing whom in each round of the draft, directly within the Discord channels.

Requirements
------------

-   Python 3.6 or newer
-   Discord account and a created bot on the Discord Developer Portal
-   Appropriate permissions for the bot to manage channels and send messages in your Discord server

Installation and Setup
----------------------

1.  Clone the repository to your local machine or server where the bot will run.

2.  Install dependencies by ensuring you have the latest version of `discord.py` library. You can install it using pip:

    `pip install discord.py`

3.  Configure your bot by creating a `.env` file in the root directory of the project. Add your Discord bot token to this file:

    `BOT_TOKEN=your_discord_bot_token_here`

4.  Run the bot by executing the main script:

    `python main.py`

Usage
-----

To start organizing a team draft event, use the following command in your Discord server:

-   `/startdraft`: Triggers the bot to open sign-ups for the draft event, pinging users with a "Cube Drafter" role, and generates a link to Draftmancer.  

- **NOTE**: This bot does not update settings in Draftmancer. It generates a unique Draftmancer link, but settings like importing the cube, and seating order (set in the next step) still need to be completed by the host. 

- Participants can interact with the bot through buttons to sign up, cancel their sign-up, or cancel the draft. After six or eight players have signed up, push "randomize teams" to create two teams and the seating order
  
- **NOTE**: Host of Draftmancer link needs to rearrange seating order in Draftmancer as per the stated seating order. The Randomize Seating button in Draftmancer must also be disabled.
  
- Once the draft is complete, push the "Draft Complete" button. This will generate three chat rooms: A combined chat with all users and two private channels for each respective team. These channels will automatically delete themselves after six hours.
  
- Once the rooms are created, push "Post Pairings". This will post round pairings into the shared chat channel.
- Enjoy your draft! 

Contributing
------------

Contributions to the Magic: The Gathering Team Draft Bot are warmly welcomed. Whether it's feature requests, bug fixes, or improvements, please feel free to fork the repository and submit a pull request. Ensure your contributions adhere to the project's licensing terms.

License
-------

This project is licensed under the GNU GENERAL PUBLIC LICENSE Version 3. This ensures that the bot, along with any derivative works, remains free and open-source, promoting a community-driven development and improvement process.

Acknowledgments
---------------

-   Thanks to everyone who has contributed to the `discord.py` project, whose tools have made this bot possible.
-   Note: Magic: The Gathering is a trademark of Wizards of the Coast LLC, a subsidiary of Hasbro, Inc. This bot is an independent project and is not affiliated with, endorsed by, or sponsored by Wizards of the Coast.
