"""
Draft data indexing for efficient querying.

Builds multiple indexes from raw Draftmancer data for fast lookup:
- Picks by (pack, pick, user_id) tuple
- Picks by pack number
- Picks by user ID
- Cards by card ID
- Players list
- Discord ↔ Draftmancer user mapping (Phase 2)
- Seat assignments from team order (Phase 2)

This is an internal class - consumers should use DraftAnalysis facade.
"""

from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
from models.draft_domain import Pick, Player, Card
from loguru import logger

if TYPE_CHECKING:
    from models import DraftSession


class DraftIndexer:
    """
    Builds and manages indexes for efficient draft querying.

    Internal class that parses raw Draftmancer JSON and builds
    multiple indexes for fast lookup. Returns domain objects.

    Phase 2: Also handles Discord ↔ Draftmancer mapping and seat assignment.
    """

    def __init__(self, draft_data: Dict, draft_session: Optional['DraftSession'] = None):
        """
        Initialize indexer from Draftmancer draft data.

        Args:
            draft_data: Raw draft data from Draftmancer/Spaces
            draft_session: Optional DraftSession for DB metadata and seating
        """
        self._session_id = draft_data.get('sessionID')
        self._draft_session = draft_session

        # Initialize index storage
        self._players: List[Player] = []
        self._players_by_id: Dict[str, Player] = {}
        self._picks_by_key: Dict[Tuple[int, int, str], Pick] = {}
        self._picks_by_pack: Dict[int, List[Pick]] = {}
        self._picks_by_user: Dict[str, List[Pick]] = {}
        self._cards: Dict[str, Card] = {}

        # Phase 2: Discord mapping
        self._discord_to_draftmancer: Dict[str, str] = {}
        self._draftmancer_to_discord: Dict[str, str] = {}

        # Build all indexes
        self._build_indexes(draft_data)

    def _build_indexes(self, draft_data: Dict):
        """
        Build all indexes from raw draft data.

        Parses Draftmancer JSON and creates:
        - Domain objects (Card, Pick, Player)
        - Lookup indexes for efficient querying
        - Discord ↔ Draftmancer mapping (Phase 2)
        - Seat assignments from team order (Phase 2)

        Args:
            draft_data: Raw draft data dictionary
        """
        # Build card index
        for card_id, card_data in draft_data.get('carddata', {}).items():
            self._cards[card_id] = Card.from_dict(card_id, card_data)

        # Phase 2: Build Discord ↔ Draftmancer mapping first
        if self._draft_session:
            self._build_discord_mapping(draft_data)

        # Phase 2: Assign seats based on team order
        seat_assignments = self._assign_seats_from_teams(draft_data)

        # Build player and pick indexes
        for user_id, user_data in draft_data.get('users', {}).items():
            # Create player with seat assignment
            seat_num = seat_assignments.get(user_id)
            if seat_num is not None:
                # Override seatNum from team order
                user_data = {**user_data, 'seatNum': seat_num}
            player = Player.from_dict(user_id, user_data)

            self._players.append(player)
            self._players_by_id[user_id] = player

            # Create picks for this player
            user_picks = []
            for pick_data in user_data.get('picks', []):
                pick = Pick.from_dict(user_id, player.user_name, pick_data)

                # Index by (pack, pick, user_id) tuple
                key = (pick.pack_num, pick.pick_num, user_id)
                self._picks_by_key[key] = pick

                # Index by pack number
                if pick.pack_num not in self._picks_by_pack:
                    self._picks_by_pack[pick.pack_num] = []
                self._picks_by_pack[pick.pack_num].append(pick)

                user_picks.append(pick)

            # Index by user ID
            self._picks_by_user[user_id] = user_picks

    def _build_discord_mapping(self, draft_data: Dict):
        """
        Build Discord ↔ Draftmancer user ID mapping.

        Maps via username matching between sign_ups and Draftmancer users.

        Args:
            draft_data: Raw draft data with users
        """
        if not self._draft_session or not self._draft_session.sign_ups:
            return

        sign_ups = self._draft_session.sign_ups
        draftmancer_users = draft_data.get('users', {})

        # Build mapping by matching usernames
        for discord_id, username in sign_ups.items():
            # sign_ups can be {'discord_id': 'username'} or {'discord_id': {'username': '...'}}
            if isinstance(username, dict):
                username = username.get('username', '')

            # Find matching Draftmancer user by username
            for dm_id, dm_user in draftmancer_users.items():
                dm_username = dm_user.get('userName', '')

                # Case-insensitive match
                if username.lower() == dm_username.lower():
                    self._discord_to_draftmancer[discord_id] = dm_id
                    self._draftmancer_to_discord[dm_id] = discord_id
                    logger.debug(f"Mapped Discord {username} → Draftmancer {dm_id}")
                    break

    def _assign_seats_from_teams(self, draft_data: Dict) -> Dict[str, int]:
        """
        Assign seat numbers based on team_a/team_b order from database.

        Seating order:
        - team_a[0] → Seat 0
        - team_b[0] → Seat 1
        - team_a[1] → Seat 2
        - team_b[1] → Seat 3
        - ... alternating

        Args:
            draft_data: Raw draft data with users

        Returns:
            Dict mapping Draftmancer user_id → seat number
        """
        seat_assignments = {}

        if not self._draft_session:
            return seat_assignments

        team_a = self._draft_session.team_a or []
        team_b = self._draft_session.team_b or []

        # Alternate between teams for seating
        seat_num = 0
        max_team_size = max(len(team_a), len(team_b))

        for i in range(max_team_size):
            # Team A player
            if i < len(team_a):
                discord_id = team_a[i]
                dm_id = self._discord_to_draftmancer.get(discord_id)
                if dm_id:
                    seat_assignments[dm_id] = seat_num
                    logger.debug(f"Assigned seat {seat_num} to {dm_id} (Team A)")
                seat_num += 1

            # Team B player
            if i < len(team_b):
                discord_id = team_b[i]
                dm_id = self._discord_to_draftmancer.get(discord_id)
                if dm_id:
                    seat_assignments[dm_id] = seat_num
                    logger.debug(f"Assigned seat {seat_num} to {dm_id} (Team B)")
                seat_num += 1

        return seat_assignments

    # === Properties ===

    def _get_session_attr(self, attr: str, default=None):
        """Get attribute from draft session if available."""
        return getattr(self._draft_session, attr, default) if self._draft_session else default

    @property
    def session_id(self) -> str:
        """Draftmancer session ID."""
        return self._session_id

    @property
    def num_players(self) -> int:
        """Number of players in draft."""
        return len(self._players)

    @property
    def has_seating(self) -> bool:
        """Whether all players have assigned seats."""
        return all(p.has_seat for p in self._players)

    @property
    def session_type(self) -> Optional[str]:
        """Session type from DB (premade, random, staked, swiss)."""
        return self._get_session_attr('session_type')

    @property
    def cube(self) -> Optional[str]:
        """Cube name from DB."""
        return self._get_session_attr('cube')

    @property
    def db_id(self) -> Optional[int]:
        """Database session ID."""
        return self._get_session_attr('id')

    @property
    def team_a(self) -> Optional[List[str]]:
        """Team A Discord IDs from DB."""
        return self._get_session_attr('team_a')

    @property
    def team_b(self) -> Optional[List[str]]:
        """Team B Discord IDs from DB."""
        return self._get_session_attr('team_b')

    # === Query Methods ===

    def get_players(self) -> List[Player]:
        """
        Get all players in draft.

        Returns:
            List of Player objects (immutable Players, so safe to return list)
        """
        return list(self._players)

    def get_pick(self, pack_num: int, pick_num: int, user_id: str) -> Optional[Pick]:
        """
        Get specific pick by pack, pick number, and user ID.

        Args:
            pack_num: Pack number (0, 1, or 2)
            pick_num: Pick number (0-14)
            user_id: Draftmancer user ID

        Returns:
            Pick object or None if not found
        """
        return self._picks_by_key.get((pack_num, pick_num, user_id))

    def get_picks_for_pack(self, pack_num: int) -> List[Pick]:
        """
        Get all picks for a specific pack.

        Args:
            pack_num: Pack number (0, 1, or 2)

        Returns:
            List of Pick objects (immutable Picks, so safe to return list)
        """
        return list(self._picks_by_pack.get(pack_num, []))

    def get_picks_for_user(self, user_id: str) -> List[Pick]:
        """
        Get all picks made by a specific player.

        Args:
            user_id: Draftmancer user ID

        Returns:
            List of Pick objects (immutable Picks, so safe to return list)
        """
        return list(self._picks_by_user.get(user_id, []))

    def get_card(self, card_id: str) -> Card:
        """
        Get card information by ID.

        Args:
            card_id: Card UUID

        Returns:
            Card object (never None - returns placeholder for unknown cards)
        """
        return self._cards.get(card_id, Card(card_id, f'Unknown Card {card_id}'))

    def get_player_by_discord_id(self, discord_id: str) -> Optional[Player]:
        """
        Get player by Discord user ID.

        Phase 2: Uses Discord ↔ Draftmancer mapping.

        Args:
            discord_id: Discord user ID

        Returns:
            Player object or None if not found
        """
        dm_id = self._discord_to_draftmancer.get(discord_id)
        if dm_id:
            return self._players_by_id.get(dm_id)
        return None

    def get_draftmancer_id(self, discord_id: str) -> Optional[str]:
        """
        Convert Discord ID to Draftmancer ID.

        Args:
            discord_id: Discord user ID

        Returns:
            Draftmancer user ID or None
        """
        return self._discord_to_draftmancer.get(discord_id)

    def get_discord_id(self, draftmancer_id: str) -> Optional[str]:
        """
        Convert Draftmancer ID to Discord ID.

        Args:
            draftmancer_id: Draftmancer user ID

        Returns:
            Discord user ID or None
        """
        return self._draftmancer_to_discord.get(draftmancer_id)

    def get_player_at_seat(self, seat_num: int) -> Optional[Player]:
        """
        Get player at specific seat number.

        Args:
            seat_num: Seat number

        Returns:
            Player object or None if seat not found
        """
        for player in self._players:
            if player.seat_num == seat_num:
                return player
        return None
