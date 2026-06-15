"""Helpers for TEST_MODE test users.

Test users get ids in a high range that never collides with real Discord
snowflakes; they are not guild members, so display paths must fall back to the
draft session's sign_ups names (see utils.generate_seating_order).
"""

TEST_USER_ID_BASE = 900000000000000000


def plan_premade_test_users(team_a, team_b, team_a_name, team_b_name,
                            team_size=3, existing_ids=None):
    """Plan test users to fill both premade teams up to team_size.

    Returns (new_users, new_team_a, new_team_b) where new_users maps each newly
    invented user id to its display name. Existing roster entries are preserved
    in order and never renamed. Ids avoid both rosters and existing_ids, so
    repeated invocations stay collision-free.
    """
    team_a = list(team_a or [])
    team_b = list(team_b or [])
    taken = set(team_a) | set(team_b) | set(existing_ids or ())
    new_users = {}

    def next_id():
        candidate = TEST_USER_ID_BASE
        while str(candidate) in taken:
            candidate += 1
        taken.add(str(candidate))
        return str(candidate)

    for team, label in ((team_a, team_a_name or "Team A"), (team_b, team_b_name or "Team B")):
        while len(team) < team_size:
            user_id = next_id()
            new_users[user_id] = f"[TEST] {label} User {len(team) + 1}"
            team.append(user_id)

    return new_users, team_a, team_b
