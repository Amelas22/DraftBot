"""
Registry for global state to prevent circular dependencies in views.py
and provide clear typing for shared state.
"""
from typing import Any, Dict, Optional
from datetime import datetime


class DraftStateManager:
    """
    Manages global state for draft sessions.
    Using a class allows for better typing and clearer usage than global dicts.
    """
    def __init__(self):
        # Maps user_id -> datetime of last ready check usage
        self.ready_check_cooldowns: Dict[str, datetime] = {}

        # Maps session_id -> bool indicating if room creation/pairings is in progress
        self.processing_rooms_pairings: Dict[str, bool] = {}

        # Maps session_id -> bool indicating if team creation is in progress
        self.processing_teams_creation: Dict[str, bool] = {}

        # Maps session_id -> ReadyCheckSession (defined in ready_check.py)
        self.ready_checks: Dict[str, Any] = {}

    # --- team creation ---

    def is_creating_teams(self, session_id: str) -> bool:
        return self.processing_teams_creation.get(session_id, False)

    def set_creating_teams(self, session_id: str, status: bool) -> None:
        if status:
            self.processing_teams_creation[session_id] = True
        elif session_id in self.processing_teams_creation:
            del self.processing_teams_creation[session_id]

    # --- room processing ---

    def is_processing_rooms(self, session_id: str) -> bool:
        return self.processing_rooms_pairings.get(session_id, False)

    def set_processing_rooms(self, session_id: str, status: bool) -> None:
        if status:
            self.processing_rooms_pairings[session_id] = True
        elif session_id in self.processing_rooms_pairings:
            del self.processing_rooms_pairings[session_id]

    # --- ready checks ---

    def get_ready_check(self, session_id: str) -> Any:
        """Return the active ReadyCheckSession for a session, or None."""
        return self.ready_checks.get(session_id)

    def set_ready_check(self, session_id: str, rc: Any) -> None:
        """Register an active ReadyCheckSession for a session."""
        self.ready_checks[session_id] = rc

    def remove_ready_check(self, session_id: str) -> None:
        """Remove the ready check for a session, clearing all associated state."""
        self.ready_checks.pop(session_id, None)

    def has_ready_check(self, session_id: str) -> bool:
        """Return True if a ready check is currently active for the session."""
        return session_id in self.ready_checks

    # --- cooldowns ---

    def get_cooldown(self, user_id: str) -> Optional[datetime]:
        return self.ready_check_cooldowns.get(user_id)

    def set_cooldown(self, user_id: str, timestamp: datetime) -> None:
        self.ready_check_cooldowns[user_id] = timestamp

    def remove_cooldown(self, user_id: str) -> None:
        if user_id in self.ready_check_cooldowns:
            del self.ready_check_cooldowns[user_id]


# Global singleton instance
state_manager = DraftStateManager()
