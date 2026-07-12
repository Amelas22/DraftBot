# Design: `/add_sub` — Grant a Substitute Access to Draft Channels

**Date:** 2026-07-09
**Status:** Approved

## Problem

When a player needs a substitute mid-draft, there is no way to give that person
access to the draft's private channels. Draft channels are created with
per-member permission overwrites (`views.py`, `create_team_channel`), so anyone
not signed up at channel-creation time cannot see the draft chat or team chat.

## Goal

A slash command that grants a substitute user access to the draft chat channel
and one team's chat channel for an active draft. Channel access only — the
sub is **not** added to sign-ups, teams, pairings, or results.

## Non-Goals

- Swapping the sub into the draft data (sign-ups, `team_a`/`team_b`, pairings).
- Persisting/tracking subs in the database (no model change, no migration).
- Removing a sub's access (admins can edit channel permissions manually; a
  `/remove_sub` counterpart can be added later if needed).

## Command

`/add_sub user:<member> [team:<A|B>]`

Defined in the `DraftCommands` cog (`cogs/draft_commands.py`), which already
hosts run-in-draft-channel commands like `/report_match`.

### Session resolution

The command must be run in one of the draft's channels. Resolve the
`DraftSession` via `DraftSession.get_by_channel_id()`; if that only matches the
main draft chat, fall back to searching sessions whose `channel_ids` JSON
contains the invoking channel's ID, so the command also works from team chats.
If no session matches, reply ephemerally that the command must be used in an
active draft channel.

### Authorization and team resolution

| Invoker | Allowed? | Team granted |
|---|---|---|
| Member of `team_a` or `team_b` | Yes | The invoker's own team (`team` param ignored) |
| Not in draft, passes admin check | Yes | Must supply `team` param; error if omitted |
| Anyone else | No — ephemeral error | — |

The admin check reuses the existing logic in `helpers/permissions.py`
(`is_bot_manager`: bot owner, configured manager role, or Manage Roles
permission).

### Channels granted

For the resolved team, apply permission overwrites for the sub on:

1. The draft chat channel (`draft_session.draft_chat_channel`).
2. The team's text chat channel (identified among `channel_ids` using the
   naming scheme `create_team_channel` uses, e.g. `Red-Team-Chat-{draft_id}` /
   `Blue-Team-Chat-{draft_id}`; the prefixes `Red-Team` and `Blue-Team` are
   hardcoded in `views.py`'s channel-creation code and do not vary with premade
   team names — `team_a_name`/`team_b_name` are used for display purposes only).
3. The team's voice channel, if one exists (premade drafts).

Overwrites match what teammates receive at channel creation
(`read_messages=True, manage_messages=True`), so the sub's experience is
identical to a team member's.

### Feedback

- **Success:** ephemeral confirmation to the invoker listing the channels
  granted, plus a public message in the draft chat:
  "@sub was added as a substitute for {team name} by @invoker".
- **Errors (all ephemeral):**
  - Not run in a draft channel.
  - Draft completed/abandoned or channels already deleted.
  - Target user is already a participant (`is_user_participating` or in
    `sign_ups`) — friendly notice, no changes made.
  - Target member cannot be resolved in the guild.
  - Discord API failure while setting permissions (report which channels
    succeeded/failed).

## Architecture

- **Pure helper** in `helpers/substitutes.py`:
  `resolve_sub_grant(session, invoker_id, is_admin, team_choice) ->
  GrantDecision | error` — decides authorization and which team applies, with
  no Discord objects involved. This is the unit-tested core.
- **Cog command** in `cogs/draft_commands.py`: resolves session + member,
  calls the helper, applies `channel.set_permissions(...)` overwrites, sends
  feedback. Uses loguru logging consistent with existing commands.

## Testing

- Unit tests in `tests/` (pytest, run via `pipenv run python -m pytest`) for
  the pure helper: player-on-team-A, player-on-team-B, admin-with-team,
  admin-without-team (error), non-participant non-admin (error), target
  already in draft (error).
- Discord interactions (permission overwrites, messages) covered with mocks
  following existing test patterns in `tests/`.
