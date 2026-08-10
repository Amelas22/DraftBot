# First-time setup (once per developer)

Read this only when the skill's preconditions are missing (no `TEST_GUILD_ID`
/ `TEST_CHANNEL` in `.env`, no test account, or the account isn't in the test
guild). All account/guild steps are USER actions done in their normal client —
Claude only guides, opens pages, and verifies afterwards. Claude must never
create accounts, enter credentials, or solve CAPTCHAs.

1. **Test guild + channel**: the dev needs a private Discord server they own,
   with a dedicated test channel (e.g. `#claude-testing` — private is best).
   To get the guild id: Discord Settings → Advanced → enable Developer Mode,
   then right-click the server icon → Copy Server ID. The DEVELOPER appends
   `TEST_GUILD_ID=<id>` and `TEST_CHANNEL=<channel name>` to `.env` themselves
   — `.env` is developer-owned and the harness denies Claude writing it. Tip
   for them: check the file ends with a newline first — a glued
   `TEST_MODE=trueTEST_…` line silently disables test mode.
2. **Bot present**: their test bot application must be in that guild (invite
   via the Developer Portal OAuth2 URL generator, scopes `bot` +
   `applications.commands`). Usually already true if they test manually.
3. **Throwaway test account**: the dev creates a fresh Discord account (e.g.
   `draftbot-tester`) themselves at https://discord.com/register — in an
   incognito/private browser window, so registration doesn't log out or
   replace their main account's session. Use a spare email. This is the
   account Claude drives; their main account is never automated.
4. **Invite it**: from their main account, generate an invite to the test
   guild (ideally single-use, the `TEST_CHANNEL` channel) and join the test
   account through it.
5. **Least-privilege mod gate**: with the bot online, run
   `/setup_bot_manager` then `/add_bot_manager @<test account>` from the
   main account — the bot creates the `Bot Manager` role with zero Discord
   permissions and assigns it. (The name must match `ADMIN_ROLE_NAME` in
   `helpers/permissions.py`: the bot grants manager access by role NAME, so
   the role needs no real permissions.) Bot-offline fallback, manual:
   Server Settings → Roles → Create Role named exactly `Bot Manager` →
   Permissions tab → **Clear Permissions** → Save → assign it to the test
   account. Either way, finally restrict the account to the `TEST_CHANNEL`
   channel via channel overrides; optionally add slowmode.
6. **Account hygiene**: on the test account — Settings → Privacy & Safety:
   disable DMs from server members and friend requests.
7. **Log in**: Claude opens `https://discord.com/login` in the in-app browser
   pane and hands off; the dev logs the TEST account in manually. The pane
   starts logged out each new Claude session, so expect to repeat this step
   per session.
8. **Verify** (Claude): the guild appears in the server sidebar, exactly the
   expected guilds are listed, the `TEST_CHANNEL` channel is visible, and the
   bot user shows in the member list. Then proceed to testing.
