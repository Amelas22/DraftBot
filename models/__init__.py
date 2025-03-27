# Import all models here for easy access and to ensure they're registered with Base
from .draft_session import DraftSession
from .match import MatchResult, Match
from .player import PlayerStats, PlayerLimit
from .team import Team, TeamRegistration, WeeklyLimit
from .challenge import Challenge, SwissChallenge
from .utility import TeamFinder
from .stake import StakeInfo

# Export all models
__all__ = [
    'DraftSession',
    'MatchResult',
    'Match',
    'PlayerStats',
    'PlayerLimit',
    'Team',
    'TeamRegistration',
    'WeeklyLimit',
    'Challenge',
    'SwissChallenge',
    'TeamFinder',
    'StakeInfo'
]
