"""
Draft Analysis - General-purpose class for analyzing MTG draft data.

Provides clean API for querying picks, understanding rotation, and accessing
draft information. Uses domain objects (Card, Pick, Player, PackTrace) for
type safety and clarity.

Example usage:
    # Load from DraftSession
    analysis = await DraftAnalysis.from_session(session)

    # Trace pack rotation
    trace = analysis.trace_pack(pack_num=0, length=4)
    for pick in trace.picks:
        print(f"{pick.user_name} picked {pick.picked_id}")

    # Query specific pick
    pick = analysis.get_pick(pack_num=0, pick_num=5, user_id="abc123")
    if pick:
        print(f"Available cards: {len(pick.booster_ids)}")
"""

from typing import Optional, List
from models.draft_domain import Pick, Player, Card, PackTrace
from models import DraftSession
from services.draft_indexer import DraftIndexer
from services.pack_tracer import PackTracer
from services.draft_data_loader import load_from_spaces


class DraftAnalysis:
    """
    Facade for draft analysis.

    Provides clean API for querying draft picks and tracing pack rotation.
    All methods return domain objects for type safety.

    Phase 2: Aggregates Draftmancer data + DB metadata for complete analysis.
    """

    def __init__(self, draft_data: dict, draft_session: Optional[DraftSession] = None):
        """
        Initialize from Draftmancer draft data.

        Phase 2: Accepts optional DraftSession for DB metadata and seating.

        Args:
            draft_data: Raw draft data from Draftmancer/Spaces
            draft_session: Optional DraftSession for DB metadata
        """
        self._indexer = DraftIndexer(draft_data, draft_session)
        self._tracer = PackTracer(self._indexer)

    @classmethod
    async def from_session(cls, session: DraftSession) -> Optional['DraftAnalysis']:
        """
        Factory: Load draft from DraftSession.

        Phase 2: Fetches draft data from Spaces AND passes DraftSession
        for DB metadata and seating information.

        Args:
            session: DraftSession with spaces_object_key

        Returns:
            DraftAnalysis instance or None if load failed
        """
        if not session.spaces_object_key:
            return None

        draft_data = await load_from_spaces(session.spaces_object_key)
        if draft_data:
            # Phase 2: Pass session for DB metadata and seating
            return cls(draft_data, draft_session=session)
        return None

    @classmethod
    async def from_spaces(cls, object_key: str) -> Optional['DraftAnalysis']:
        """
        Factory: Load draft directly from Spaces.

        Args:
            object_key: Spaces object path (e.g., "team/PowerLSV-123.json")

        Returns:
            DraftAnalysis instance or None if load failed
        """
        draft_data = await load_from_spaces(object_key)
        if draft_data:
            return cls(draft_data)
        return None

    # === Properties ===

    @property
    def session_id(self) -> str:
        """Draftmancer session ID."""
        return self._indexer.session_id

    @property
    def num_players(self) -> int:
        """Number of players in draft."""
        return self._indexer.num_players

    @property
    def has_seating(self) -> bool:
        """Whether draft has seating assignments."""
        return self._indexer.has_seating

    @property
    def session_type(self) -> Optional[str]:
        """Session type from DB (premade, random, staked, swiss). Phase 2."""
        return self._indexer.session_type

    @property
    def cube(self) -> Optional[str]:
        """Cube name from DB. Phase 2."""
        return self._indexer.cube

    @property
    def db_id(self) -> Optional[int]:
        """Database session ID. Phase 2."""
        return self._indexer.db_id

    @property
    def team_a(self) -> Optional[List[str]]:
        """Team A Discord IDs from DB. Phase 2."""
        return self._indexer.team_a

    @property
    def team_b(self) -> Optional[List[str]]:
        """Team B Discord IDs from DB. Phase 2."""
        return self._indexer.team_b

    # === Query Methods ===

    def trace_pack(self, pack_num: int, length: int = 4, debug: bool = False) -> Optional[PackTrace]:
        """
        Trace pack through rotation by matching booster contents.

        Finds a sequence of N consecutive picks by matching when
        booster[n+1] == booster[n] - picked_card.

        Args:
            pack_num: Pack number (0, 1, or 2)
            length: Number of consecutive picks to trace (default 4)
            debug: If True, log detailed debugging info

        Returns:
            PackTrace with picks in order, or None if sequence not found
        """
        return self._tracer.trace_pack(pack_num, length, debug)

    def get_pick(self, pack_num: int, pick_num: int, user_id: str) -> Optional[Pick]:
        """
        Get specific pick.

        Args:
            pack_num: Pack number (0, 1, or 2)
            pick_num: Pick number (0-14)
            user_id: Draftmancer user ID

        Returns:
            Pick object or None if not found
        """
        return self._indexer.get_pick(pack_num, pick_num, user_id)

    def get_player_picks(self, user_id: str) -> List[Pick]:
        """
        Get all picks for a player.

        Args:
            user_id: Draftmancer user ID

        Returns:
            List of Pick objects
        """
        return self._indexer.get_picks_for_user(user_id)

    def get_card(self, card_id: str) -> Card:
        """
        Get card information.

        Args:
            card_id: Card UUID

        Returns:
            Card object (never None, returns placeholder for unknown cards)
        """
        return self._indexer.get_card(card_id)

    def get_players(self) -> List[Player]:
        """
        Get all players in draft.

        Returns:
            List of Player objects
        """
        return self._indexer.get_players()

    def get_picks_for_pack(self, pack_num: int) -> List[Pick]:
        """
        Get all picks for a specific pack.

        Note: In a multi-player draft, multiple players pick simultaneously
        at each pick number. For example, in a 6-player draft, there will be
        6 picks at pick_num=0, 6 picks at pick_num=1, etc.

        Args:
            pack_num: Pack number (0, 1, or 2)

        Returns:
            List of Pick objects, sorted by pick_num then user_name
        """
        picks = self._indexer.get_picks_for_pack(pack_num)
        # Sort by pick number, then by user name for consistency
        picks.sort(key=lambda p: (p.pick_num, p.user_name))
        return picks

    def get_carddata(self) -> dict:
        """
        Get the carddata dictionary for image lookups.

        Returns:
            Dictionary mapping card UUIDs to card information including Scryfall image URLs
        """
        return self._indexer._data.get("carddata", {})

    def get_picks_at(self, pack_num: int, pick_num: int) -> List[Pick]:
        """
        Get all picks at a specific pack and pick number.

        In a multi-player draft, multiple players pick simultaneously.
        This returns ALL picks made at this pick number.

        Example: In a 6-player draft, get_picks_at(0, 5) returns 6 Pick
        objects - one for each player's 5th pick in pack 0.

        Args:
            pack_num: Pack number (0, 1, or 2)
            pick_num: Pick number (0-14)

        Returns:
            List of Pick objects for all players at this pick number
        """
        all_picks = self.get_picks_for_pack(pack_num)
        return [p for p in all_picks if p.pick_num == pick_num]

    def get_player_by_discord_id(self, discord_id: str) -> Optional[Player]:
        """
        Get player by Discord user ID. Phase 2.

        Uses Discord ↔ Draftmancer mapping built from sign_ups.

        Args:
            discord_id: Discord user ID

        Returns:
            Player object or None if not found
        """
        return self._indexer.get_player_by_discord_id(discord_id)

    def get_player_at_seat(self, seat_num: int) -> Optional[Player]:
        """
        Get player at specific seat number. Phase 2.

        Args:
            seat_num: Seat number

        Returns:
            Player object or None
        """
        return self._indexer.get_player_at_seat(seat_num)

    def __repr__(self):
        return f"<DraftAnalysis(session={self.session_id}, players={self.num_players})>"
