"""
DraftAnalysis Class - Behavioral Specification (TDD)

Phase 1 Implementation: Core analysis features with domain objects
Phase 2 (Deferred): DB metadata aggregation, Discord mapping
"""

import pytest
from services.draft_analysis import DraftAnalysis
from models.draft_domain import Pick, Player, Card, PackTrace


def create_mock_draft_data():
    """Create mock draft data for testing"""
    return {
        'sessionID': 'TEST_SESSION_123',
        'users': {
            'user1': {
                'userName': 'Alice',
                'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['card1', 'card2', 'card3']},
                    {'packNum': 1, 'pickNum': 0, 'pick': [1], 'booster': ['card4', 'card5']},
                ]
            },
            'user2': {
                'userName': 'Bob',
                'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['card2', 'card3']},
                ]
            },
        },
        'carddata': {
            'card1': {'name': 'Lightning Bolt'},
            'card2': {'name': 'Counterspell'},
            'card3': {'name': 'Dark Ritual'},
            'card4': {'name': 'Swords to Plowshares'},
            'card5': {'name': 'Brainstorm'},
        }
    }


class TestDraftAnalysisConstruction:
    """Construction and data loading - Phase 1"""

    def test_should_construct_from_draft_data_directly(self):
        """
        Should support direct construction from draft_data dict.
        DraftAnalysis(draft_data) -> for testing/when data already loaded
        """
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        assert analysis is not None
        assert analysis.session_id == 'TEST_SESSION_123'
        assert analysis.num_players == 2

    @pytest.mark.asyncio
    async def test_should_construct_from_draft_session_object(self):
        """
        Should construct from a DraftSession DB object.
        DraftAnalysis.from_session(draft_session) -> fetches spaces_object_key, loads data

        Note: This test requires mocking Spaces fetch, skipped for now
        """
        pytest.skip("Requires mocking DigitalOcean Spaces fetch")

    @pytest.mark.asyncio
    async def test_should_fetch_from_spaces_automatically(self):
        """
        When constructed from DraftSession with spaces_object_key,
        should automatically fetch draft_data from DigitalOcean Spaces.

        Note: This test requires mocking Spaces fetch, skipped for now
        """
        pytest.skip("Requires mocking DigitalOcean Spaces fetch")

    def test_should_aggregate_db_and_draftmancer_data(self):
        """
        Should combine:
        - Draftmancer data (users, picks, carddata)
        - DB data (session_type, cube, teams, sign_ups)

        Phase 2: Implemented!
        """
        from models import DraftSession

        # Create mock DraftSession
        mock_session = DraftSession(
            id=123,
            session_id='db_session_123',
            session_type='random',
            cube='TestCube',
            team_a=['discord_alice'],
            team_b=['discord_bob'],
            sign_ups={'discord_alice': 'Alice', 'discord_bob': 'Bob'}
        )

        draft_data = create_mock_draft_data()

        # Create analysis with both Draftmancer + DB data
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        # Should have both Draftmancer data
        assert analysis.session_id == 'TEST_SESSION_123'  # From Draftmancer
        assert analysis.num_players == 2

        # AND DB metadata
        assert analysis.session_type == 'random'  # From DB
        assert analysis.cube == 'TestCube'  # From DB
        assert analysis.db_id == 123  # From DB


class TestDraftMetadata:
    """Access to draft metadata - Phase 1 (partial)"""

    def test_should_expose_session_id(self):
        """Should expose Draftmancer sessionID"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        assert analysis.session_id == 'TEST_SESSION_123'

    def test_should_expose_player_count(self):
        """Should return number of players"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        assert analysis.num_players == 2

    def test_should_expose_session_type(self):
        """Should expose session_type (premade, random, staked, swiss). Phase 2."""
        from models import DraftSession

        mock_session = DraftSession(
            id=1,
            session_id='test',
            session_type='staked',
            sign_ups={}
        )

        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        assert analysis.session_type == 'staked'

    def test_should_expose_cube_name(self):
        """Should expose cube name from DB. Phase 2."""
        from models import DraftSession

        mock_session = DraftSession(
            id=1,
            session_id='test',
            cube='Vintage Cube',
            sign_ups={}
        )

        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        assert analysis.cube == 'Vintage Cube'

    def test_should_map_discord_users_to_draftmancer_users(self):
        """
        Should map Discord user IDs (from sign_ups) to Draftmancer user IDs.
        Phase 2: Implemented!
        """
        from models import DraftSession

        mock_session = DraftSession(
            id=1,
            session_id='test',
            team_a=['discord_alice'],
            team_b=['discord_bob'],
            sign_ups={'discord_alice': 'Alice', 'discord_bob': 'Bob'}
        )

        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        # Should be able to look up players by Discord ID
        alice = analysis.get_player_by_discord_id('discord_alice')
        assert alice is not None
        assert alice.user_name == 'Alice'

        bob = analysis.get_player_by_discord_id('discord_bob')
        assert bob is not None
        assert bob.user_name == 'Bob'


class TestPickQuerying:
    """Methods for querying specific picks - Phase 1"""

    def test_should_get_pick_by_pack_pick_and_player(self):
        """Should retrieve a specific pick given pack#, pick#, and player identifier"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        pick = analysis.get_pick(pack_num=0, pick_num=0, user_id='user1')

        assert pick is not None
        assert isinstance(pick, Pick)
        assert pick.user_name == 'Alice'
        assert pick.pack_num == 0
        assert pick.pick_num == 0
        assert pick.picked_id == 'card1'  # Index 0 of booster

    def test_should_return_none_for_nonexistent_pick(self):
        """Should return None when querying a pick that doesn't exist"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        pick = analysis.get_pick(pack_num=99, pick_num=99, user_id='user999')

        assert pick is None

    def test_should_get_booster_contents_for_pick(self):
        """Should return list of card IDs available in a booster at a specific pick"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        pick = analysis.get_pick(pack_num=0, pick_num=0, user_id='user1')

        assert pick is not None
        assert pick.booster_ids == ['card1', 'card2', 'card3']

    def test_should_get_picked_card_for_pick(self):
        """Should return which card was actually picked (as card ID, not index)"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        pick = analysis.get_pick(pack_num=0, pick_num=0, user_id='user1')

        assert pick is not None
        assert pick.picked_id == 'card1'  # Card ID, not index


class TestPackTracing:
    """Pack tracing through draft rotation - Phase 1"""

    def test_should_trace_consecutive_picks_in_same_pack(self):
        """
        Should trace the pack as it passes between players
        by matching booster contents (booster[n+1] == booster[n] - picked_card)
        """
        # Create mock data with traceable pack
        draft_data = {
            'sessionID': 'TEST',
            'users': {
                'user1': {
                    'userName': 'Alice',
                    'picks': [
                        {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1', 'c2', 'c3']},
                    ]
                },
                'user2': {
                    'userName': 'Bob',
                    'picks': [
                        # After Alice picked c1
                        {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c2', 'c3']},
                    ]
                },
            },
            'carddata': {'c1': {'name': 'Card1'}, 'c2': {'name': 'Card2'}, 'c3': {'name': 'Card3'}}
        }

        analysis = DraftAnalysis(draft_data)
        trace = analysis.trace_pack(pack_num=0, length=2)

        assert trace is not None
        assert isinstance(trace, PackTrace)
        assert len(trace.picks) == 2
        assert trace.picks[0].user_name == 'Alice'
        assert trace.picks[1].user_name == 'Bob'

    def test_should_handle_pack_size_limit(self):
        """Should skip/reject packs with >25 cards (Discord UI limit)"""
        draft_data = {
            'sessionID': 'TEST',
            'users': {
                'user1': {
                    'userName': 'Alice',
                    'picks': [
                        # 30 cards - too large
                        {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': [f'card{i}' for i in range(30)]},
                    ]
                },
            },
            'carddata': {}
        }

        analysis = DraftAnalysis(draft_data)
        trace = analysis.trace_pack(pack_num=0, length=1)

        assert trace is None  # Should skip oversized packs

    def test_should_trace_different_pack_lengths(self):
        """Should be able to trace 2, 3, 4, or N consecutive picks"""
        draft_data = {
            'sessionID': 'TEST',
            'users': {
                'user1': {'userName': 'P1', 'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1','c2','c3','c4']},
                ]},
                'user2': {'userName': 'P2', 'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c2','c3','c4']},
                ]},
                'user3': {'userName': 'P3', 'picks': [
                    {'packNum': 0, 'pickNum': 2, 'pick': [0], 'booster': ['c3','c4']},
                ]},
            },
            'carddata': {}
        }

        analysis = DraftAnalysis(draft_data)

        # Try length 2
        trace2 = analysis.trace_pack(pack_num=0, length=2)
        assert trace2 is not None
        assert len(trace2.picks) == 2

        # Try length 3
        trace3 = analysis.trace_pack(pack_num=0, length=3)
        assert trace3 is not None
        assert len(trace3.picks) == 3

    def test_should_return_none_when_sequence_breaks(self):
        """Should return None if pack passing can't be traced (missing picks, etc.)"""
        draft_data = {
            'sessionID': 'TEST',
            'users': {
                'user1': {
                    'userName': 'Alice',
                    'picks': [
                        {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1', 'c2']},
                    ]
                },
                # No user2 - sequence breaks
            },
            'carddata': {}
        }

        analysis = DraftAnalysis(draft_data)
        trace = analysis.trace_pack(pack_num=0, length=2)

        assert trace is None

    def test_traced_picks_should_have_correct_order(self):
        """Traced picks should be in chronological order (pick 0, 1, 2, 3)"""
        draft_data = {
            'sessionID': 'TEST',
            'users': {
                'user1': {'userName': 'P1', 'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1','c2','c3']},
                ]},
                'user2': {'userName': 'P2', 'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c2','c3']},
                ]},
            },
            'carddata': {}
        }

        analysis = DraftAnalysis(draft_data)
        trace = analysis.trace_pack(pack_num=0, length=2)

        assert trace is not None
        assert trace.picks[0].pick_num == 0
        assert trace.picks[1].pick_num == 1
        # Convenience property should also work
        assert trace.pick_numbers == [0, 1]


class TestPlayerInformation:
    """Methods for accessing player/user information - Phase 1"""

    def test_should_get_all_player_names(self):
        """Should return list of all player names in the draft"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        players = analysis.get_players()

        assert len(players) == 2
        assert all(isinstance(p, Player) for p in players)
        player_names = [p.user_name for p in players]
        assert 'Alice' in player_names
        assert 'Bob' in player_names

    def test_should_map_player_to_picks(self):
        """Should retrieve all picks made by a specific player"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        picks = analysis.get_player_picks('user1')

        assert len(picks) == 2
        assert all(isinstance(p, Pick) for p in picks)
        assert all(p.user_name == 'Alice' for p in picks)

    def test_should_identify_player_from_pick(self):
        """Given a pick, should return which player made it"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        pick = analysis.get_pick(pack_num=0, pick_num=0, user_id='user1')

        assert pick is not None
        assert pick.user_name == 'Alice'
        assert pick.user_id == 'user1'


class TestCardData:
    """Card information and lookup - Phase 1"""

    def test_should_lookup_card_names_from_ids(self):
        """Should convert card UUIDs to readable card names"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        card = analysis.get_card('card1')

        assert isinstance(card, Card)
        assert card.name == 'Lightning Bolt'
        assert card.id == 'card1'

    def test_should_handle_missing_card_data(self):
        """Should gracefully handle cards not in carddata"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        card = analysis.get_card('unknown_card_999')

        assert isinstance(card, Card)
        assert 'Unknown' in card.name
        assert '999' in card.name

    def test_should_access_full_card_info(self):
        """Should retrieve complete card data from card ID"""
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        card = analysis.get_card('card2')

        assert card.id == 'card2'
        assert card.name == 'Counterspell'


class TestDraftStructure:
    """Understanding draft structure and metadata - DEFERRED TO PHASE 2"""

    def test_should_identify_num_packs(self):
        """DEFERRED TO PHASE 2: Draft structure analysis"""
        pytest.skip("Phase 2 feature: pack count analysis")

    def test_should_identify_num_picks_per_pack(self):
        """DEFERRED TO PHASE 2: Draft structure analysis"""
        pytest.skip("Phase 2 feature: picks per pack analysis")

    def test_should_group_picks_by_pack(self):
        """
        Should group all picks by pack number for analysis

        NOTE: This is partially implemented - we can trace packs,
        but don't expose a direct "get all picks for pack" method yet.
        """
        draft_data = create_mock_draft_data()
        analysis = DraftAnalysis(draft_data)

        # We can get picks via trace_pack, but no direct "get_picks_by_pack" method yet
        # This is okay for Phase 1 - can add in Phase 2 if needed
        pytest.skip("Phase 2 feature: comprehensive pack grouping API")


class TestRotationLogic:
    """Pack rotation and passing logic - OPTIONAL/DEFERRED"""

    def test_should_identify_rotation_direction_per_pack(self):
        """Pack 0/2 pass left, Pack 1 passes right (if seating exists)"""
        pytest.skip("Optional feature: rotation direction (rarely needed)")


class TestEdgeCases:
    """Edge cases and error handling - DEFERRED TO PHASE 2"""

    def test_should_handle_draft_with_no_picks(self):
        """Should handle drafts where no picks have been made yet"""
        pytest.skip("Phase 2 feature: edge case handling")

    def test_should_handle_incomplete_packs(self):
        """Should handle packs with <15 picks"""
        pytest.skip("Phase 2 feature: edge case handling")

    def test_should_handle_malformed_pick_data(self):
        """Should handle picks with missing fields (no booster, no pick, etc.)"""
        pytest.skip("Phase 2 feature: edge case handling")
