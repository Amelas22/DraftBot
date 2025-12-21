"""
Pack tracing logic via booster matching.

Traces packs through draft rotation by matching booster contents.
Works WITHOUT needing seating information - matches when:
  booster[n+1] == booster[n] - picked_card

This is the core algorithm that makes quiz generation possible.
"""

from typing import List, Optional, Set
from models.draft_domain import Pick, PackTrace
from services.draft_indexer import DraftIndexer
from loguru import logger


class PackTracer:
    """
    Traces pack rotation via booster matching.

    Uses clever algorithm that matches booster contents to find
    how packs rotated between players, without requiring seating data.
    """

    # Discord Select UI has 25-option limit
    MAX_PACK_SIZE = 25

    # Limit attempts for performance
    MAX_START_ATTEMPTS = 20

    def __init__(self, indexer: DraftIndexer):
        """
        Initialize tracer with draft indexer.

        Args:
            indexer: DraftIndexer with built indexes
        """
        self._indexer = indexer

    def trace_pack(self, pack_num: int, length: int = 4, debug: bool = False) -> Optional[PackTrace]:
        """
        Trace pack through picks.

        Phase 2: Uses seat-based tracing when seating is available (reliable!).
        Falls back to booster matching when seating doesn't exist.

        Seat-based tracing:
        - Pack 0/2: Pass left (seat → seat+1)
        - Pack 1: Pass right (seat → seat-1)
        - Deterministic and reliable

        Booster matching (fallback):
        - Match when booster[n+1] == booster[n] - picked_card
        - Fragile (breaks with card swapping, burns, etc.)
        - Use only when seating unavailable

        Args:
            pack_num: Pack number (0, 1, or 2)
            length: Number of consecutive picks to trace (default 4)
            debug: If True, log detailed debugging info

        Returns:
            PackTrace with picks in order, or None if not found
        """
        # Phase 2: Use seat-based tracing if seating is available
        if self._indexer.has_seating:
            if debug:
                logger.debug(f"Using seat-based tracing for pack {pack_num}")
            chain = self._trace_by_seats(pack_num, length, debug)
        else:
            if debug:
                logger.debug(f"No seating available, using booster matching for pack {pack_num}")
            chain = self._trace_by_booster_matching(pack_num, length, debug)

        if chain:
            if debug:
                logger.debug(f"Found {length}-pick chain for pack {pack_num}")
            return PackTrace(pack_num, chain)

        if debug:
            logger.debug(f"No {length}-pick chain found for pack {pack_num}")
        return None

    def _trace_by_seats(self, pack_num: int, length: int, debug: bool = False) -> Optional[List[Pick]]:
        """
        Trace pack using seat-based rotation (Phase 2).

        This is the reliable method! Uses known seating order:
        - Pack 0/2: Pass left (seat → seat+1)
        - Pack 1: Pass right (seat → seat-1)

        Args:
            pack_num: Pack number
            length: Chain length
            debug: Debug logging

        Returns:
            List of Pick objects or None
        """
        num_players = self._indexer.num_players

        # Try each seat as starting point
        for start_seat in range(num_players):
            chain = []
            current_seat = start_seat
            pick_num = 0

            # Build chain by following seat rotation
            for step in range(length):
                # Get player at current seat
                player = self._indexer.get_player_at_seat(current_seat)
                if not player:
                    break

                # Get their pick at this pick number
                pick = self._indexer.get_pick(pack_num, pick_num, player.user_id)
                if not pick:
                    break

                # Skip oversized packs (Discord limit)
                if step == 0 and pick.booster_size > self.MAX_PACK_SIZE:
                    break

                chain.append(pick)

                # Calculate next seat based on rotation
                current_seat = self._get_next_seat(current_seat, pack_num, num_players)
                pick_num += 1

            # If we found a complete chain, return it
            if len(chain) == length:
                if debug:
                    logger.debug(f"Seat-based trace: {[p.user_name for p in chain]}")
                return chain

        return None

    def _get_next_seat(self, current_seat: int, pack_num: int, num_players: int) -> int:
        """
        Calculate next seat based on pack rotation.

        Pack rotation:
        - Pack 0 (even): Pass LEFT (seat → seat+1)
        - Pack 1 (odd): Pass RIGHT (seat → seat-1)
        - Pack 2 (even): Pass LEFT (seat → seat+1)

        Args:
            current_seat: Current seat number
            pack_num: Pack number
            num_players: Total number of players

        Returns:
            Next seat number (wraps around)
        """
        if pack_num % 2 == 0:  # Pack 0, 2 - pass left
            return (current_seat + 1) % num_players
        else:  # Pack 1 - pass right
            return (current_seat - 1) % num_players

    def _trace_by_booster_matching(self, pack_num: int, length: int, debug: bool = False) -> Optional[List[Pick]]:
        """
        Trace pack by matching booster contents (fallback method).

        WARNING: Fragile! Breaks with card swapping, burns, etc.
        Only use when seating information is unavailable.

        Args:
            pack_num: Pack number
            length: Chain length
            debug: Debug logging

        Returns:
            List of Pick objects or None
        """
        picks = self._indexer.get_picks_for_pack(pack_num)

        if debug:
            logger.debug(f"Booster matching: {len(picks)} picks to search")

        chain = self._find_matching_chain(picks, length, debug)
        return chain

    def _find_matching_chain(self, picks: List[Pick], length: int, debug: bool = False) -> Optional[List[Pick]]:
        """
        Find chain of N picks by matching booster contents.

        Tries each pick as a starting point and attempts to build
        a chain of specified length.

        Args:
            picks: All picks for a pack
            length: Desired chain length
            debug: Enable debug logging

        Returns:
            List of Pick objects in order, or None
        """
        # Try each pick as starting point (limit to MAX_START_ATTEMPTS)
        for start_idx, start_pick in enumerate(picks[:self.MAX_START_ATTEMPTS]):
            # Skip oversized packs (Discord limit)
            if start_pick.booster_size > self.MAX_PACK_SIZE:
                continue

            # Skip picks without valid picked card
            if start_pick.picked_id is None:
                continue

            if debug:
                logger.debug(
                    f"Attempt {start_idx + 1}: Start with {start_pick.user_name} "
                    f"pick#{start_pick.pick_num} ({start_pick.booster_size} cards)"
                )

            # Try to build chain from this start
            chain = self._build_chain_from(start_pick, picks, length, debug)

            if chain and len(chain) == length:
                if debug:
                    logger.debug(f"SUCCESS! Found {length}-pick chain")
                return chain

        return None

    def _build_chain_from(
        self,
        start: Pick,
        all_picks: List[Pick],
        length: int,
        debug: bool = False
    ) -> Optional[List[Pick]]:
        """
        Build chain starting from a specific pick.

        Args:
            start: Starting pick
            all_picks: All available picks
            length: Desired chain length
            debug: Enable debug logging

        Returns:
            List of Pick objects or None if chain breaks
        """
        chain = [start]
        expected_booster = self._get_remaining_cards(start)

        if debug:
            logger.debug(f"Expected next: {len(expected_booster)} cards")

        # Find remaining picks
        for step in range(length - 1):
            next_pick = self._find_matching_pick(expected_booster, all_picks, chain)

            if not next_pick:
                if debug:
                    logger.debug(f"Step {step + 1}: No match found")
                break

            chain.append(next_pick)
            expected_booster = self._get_remaining_cards(next_pick)

            if debug:
                logger.debug(
                    f"Step {step + 1}: Found {next_pick.user_name} "
                    f"pick#{next_pick.pick_num}"
                )

        return chain if len(chain) == length else None

    def _get_remaining_cards(self, pick: Pick) -> Set[str]:
        """
        Get cards remaining after this pick (booster minus picked card).

        Args:
            pick: Current pick

        Returns:
            Set of card IDs remaining in booster after pick
        """
        return set(pick.booster_ids) - {pick.picked_id}

    def _find_matching_pick(
        self,
        expected: Set[str],
        candidates: List[Pick],
        chain: List[Pick]
    ) -> Optional[Pick]:
        """
        Find pick with matching booster (not already in chain).

        Args:
            expected: Expected set of card IDs
            candidates: All available picks
            chain: Current chain (to avoid duplicates)

        Returns:
            Pick with matching booster, or None
        """
        for candidate in candidates:
            # Skip if already in chain
            if self._is_in_chain(candidate, chain):
                continue

            # Check if booster matches
            if set(candidate.booster_ids) == expected:
                # Only return if pick has valid picked_id
                if candidate.picked_id is not None:
                    return candidate

        return None

    def _is_in_chain(self, pick: Pick, chain: List[Pick]) -> bool:
        """
        Check if pick is already in chain.

        Args:
            pick: Pick to check
            chain: Current chain

        Returns:
            True if pick is in chain
        """
        return any(
            p.user_id == pick.user_id and p.pick_num == pick.pick_num
            for p in chain
        )
