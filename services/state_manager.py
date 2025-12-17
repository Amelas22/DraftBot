"""
Registry for global state to prevent circular dependencies in views.py
and provide clear typing for shared state.
"""
from typing import Dict, Any, Optional
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
        
        # Maps session_id -> Dict containing ready check state/data
        # (Legacy: Was just a dict in views.py, exact type depends on usage in ReadyCheckView)
        self.sessions: Dict[str, Any] = {}

    def is_creating_teams(self, session_id: str) -> bool:
        """Check if teams are currently being created for a session."""
        return self.processing_teams_creation.get(session_id, False)

    def set_creating_teams(self, session_id: str, status: bool) -> None:
        """Set the team creation status for a session."""
        if status:
            self.processing_teams_creation[session_id] = True
        elif session_id in self.processing_teams_creation:
            del self.processing_teams_creation[session_id]

    def is_processing_rooms(self, session_id: str) -> bool:
        """Check if rooms/pairings are currently being processed for a session."""
        return self.processing_rooms_pairings.get(session_id, False)

    def set_processing_rooms(self, session_id: str, status: bool) -> None:
        """Set the room processing status for a session."""
        if status:
            self.processing_rooms_pairings[session_id] = True
        elif session_id in self.processing_rooms_pairings:
            del self.processing_rooms_pairings[session_id]
            
    def get_ready_check_session(self, session_id: str) -> Optional[Any]:
        """Get the ready check session data."""
        return self.sessions.get(session_id)
        
    def set_ready_check_session(self, session_id: str, data: Any) -> None:
        """Set the ready check session data."""
        self.sessions[session_id] = data
        
    def remove_ready_check_session(self, session_id: str) -> None:
        """Remove a ready check session."""
        if session_id in self.sessions:
            del self.sessions[session_id]

    def session_exists(self, session_id: str) -> bool:
        """Check if a ready check session exists."""
        return session_id in self.sessions

    def get_cooldown(self, user_id: str) -> Optional[datetime]:
        """Get the cooldown expiration time for a user."""
        return self.ready_check_cooldowns.get(user_id)
        
    def set_cooldown(self, user_id: str, timestamp: datetime) -> None:
        """Set the cooldown expiration time for a user."""
        self.ready_check_cooldowns[user_id] = timestamp

    def remove_cooldown(self, user_id: str) -> None:
        """Remove a cooldown for a user."""
        if user_id in self.ready_check_cooldowns:
            del self.ready_check_cooldowns[user_id]

# Global singleton instance
state_manager = DraftStateManager()
