"""Pure seating helpers, kept dependency-free so they're fast to import and test
(the DraftSetupManager module that uses this is heavy to import)."""
from collections import defaultdict, deque


def resolve_seating_ids(session_users, desired_username_order, bot_id):
    """Map a desired username order to Draftmancer userIDs, robust to duplicate
    display names.

    Each occurrence of a name consumes a DISTINCT userID (in session order),
    instead of a name->id dict that collapses duplicates — which would seat one
    userID twice and omit the other same-named player. The bot is excluded.

    Returns (user_id_order, missing_users).
    """
    by_name = defaultdict(deque)
    for user in session_users:
        user_id = user.get("userID")
        username = user.get("userName")
        if user_id != bot_id and username:
            by_name[username].append(user_id)

    user_id_order = []
    missing_users = []
    for username in desired_username_order:
        queue = by_name.get(username)
        if queue:
            user_id_order.append(queue.popleft())
        else:
            missing_users.append(username)
    return user_id_order, missing_users
