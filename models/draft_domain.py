"""
Domain objects for draft analysis.

Immutable dataclasses representing core entities in an MTG draft:
- Card: A Magic card with ID and name
- Pick: A single pick made by a player
- Player: A player in the draft
- PackTrace: A sequence of picks showing pack rotation
"""

from dataclasses import dataclass
from typing import List, Optional, Dict


@dataclass(frozen=True)
class Card:
    """
    Immutable card representation.

    Attributes:
        id: Card UUID from Draftmancer
        name: Human-readable card name
    """
    id: str
    name: str

    @classmethod
    def from_dict(cls, card_id: str, card_data: Dict) -> 'Card':
        """
        Create Card from Draftmancer carddata entry.

        Args:
            card_id: Card UUID
            card_data: Dictionary with card info (must have 'name' key)

        Returns:
            Card instance with id and name
        """
        return cls(
            id=card_id,
            name=card_data.get('name', f'Unknown Card {card_id}')
        )

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Pick:
    """
    Immutable pick representation.

    Represents a single pick made by a player during the draft.
    The picked card is identified by UUID (not by index).

    Attributes:
        user_id: Draftmancer user ID
        user_name: Player's display name
        pack_num: Pack number (0, 1, or 2)
        pick_num: Pick number within pack (0-14)
        booster_ids: List of card UUIDs available in booster
        picked_id: UUID of card that was picked
    """
    user_id: str
    user_name: str
    pack_num: int
    pick_num: int
    booster_ids: List[str]  # Card UUIDs in booster
    picked_id: str          # UUID of picked card

    @property
    def booster_size(self) -> int:
        """Number of cards in booster."""
        return len(self.booster_ids)

    def contains_card(self, card_id: str) -> bool:
        """Check if booster contained a specific card."""
        return card_id in self.booster_ids

    @classmethod
    def from_dict(cls, user_id: str, user_name: str, pick_data: Dict) -> 'Pick':
        """
        Create Pick from Draftmancer pick data.

        IMPORTANT: Converts picked INDEX to picked UUID.
        Draftmancer stores 'pick' as an index into 'booster' list.
        We resolve this to the actual card UUID.

        Args:
            user_id: Draftmancer user ID
            user_name: Player's display name
            pick_data: Dictionary with pack_num, pick_num, booster, pick keys

        Returns:
            Pick instance with resolved picked_id
        """
        booster = pick_data.get('booster', [])
        picked_list = pick_data.get('pick', [])

        # picked is a LIST with single index value
        picked_index = picked_list[0] if picked_list else None

        # Resolve index to actual card UUID
        if picked_index is not None and picked_index < len(booster):
            picked_id = booster[picked_index]
        else:
            picked_id = None  # Invalid or missing pick

        return cls(
            user_id=user_id,
            user_name=user_name,
            pack_num=pick_data.get('packNum'),
            pick_num=pick_data.get('pickNum'),
            booster_ids=booster,
            picked_id=picked_id
        )


@dataclass(frozen=True)
class Player:
    """
    Player information.

    Attributes:
        user_id: Draftmancer user ID
        user_name: Player's display name
        seat_num: Seat number (optional, may not exist for all draft types)
    """
    user_id: str
    user_name: str
    seat_num: Optional[int] = None

    @property
    def has_seat(self) -> bool:
        """Whether player has assigned seat number."""
        return self.seat_num is not None

    @classmethod
    def from_dict(cls, user_id: str, user_data: Dict) -> 'Player':
        """
        Create Player from Draftmancer user data.

        Args:
            user_id: Draftmancer user ID
            user_data: Dictionary with userName and optional seatNum

        Returns:
            Player instance
        """
        return cls(
            user_id=user_id,
            user_name=user_data.get('userName', 'Unknown'),
            seat_num=user_data.get('seatNum')
        )


@dataclass(frozen=True)
class PackTrace:
    """
    Result of pack tracing.

    Represents a sequence of consecutive picks showing how a pack
    rotated between players.

    Attributes:
        pack_num: Pack number (0, 1, or 2)
        picks: List of Pick objects in chronological order
    """
    pack_num: int
    picks: List[Pick]

    def __len__(self) -> int:
        """Number of picks in trace."""
        return len(self.picks)

    @property
    def player_names(self) -> List[str]:
        """Names of players who made picks in order."""
        return [p.user_name for p in self.picks]

    @property
    def pick_numbers(self) -> List[int]:
        """Pick numbers in chronological order."""
        return [p.pick_num for p in self.picks]

    @property
    def picked_ids(self) -> List[str]:
        """Card UUIDs that were picked in order."""
        return [p.picked_id for p in self.picks]
