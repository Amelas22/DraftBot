"""
Tests for quiz seat functionality.

Tests the starting_seat parameter in pack tracing and the
get_valid_starting_seats() method.
"""

import pytest
from services.draft_analysis import DraftAnalysis
from services.pack_tracer import PackTracer
from services.draft_indexer import DraftIndexer
from models import DraftSession


def create_6_player_draft_data():
    """
    Create mock 6-player draft data with seating assignments.
    Pack 0 passes left (seat 0 -> 1 -> 2 -> 3 -> 4 -> 5).
    """
    return {
        'sessionID': 'TEST_6P_SESSION',
        'users': {
            'user0': {
                'userName': 'Player0',
                'seatNum': 0,
                'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1', 'c2', 'c3', 'c4', 'c5', 'c6']},
                ]
            },
            'user1': {
                'userName': 'Player1',
                'seatNum': 1,
                'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c2', 'c3', 'c4', 'c5', 'c6']},
                ]
            },
            'user2': {
                'userName': 'Player2',
                'seatNum': 2,
                'picks': [
                    {'packNum': 0, 'pickNum': 2, 'pick': [0], 'booster': ['c3', 'c4', 'c5', 'c6']},
                ]
            },
            'user3': {
                'userName': 'Player3',
                'seatNum': 3,
                'picks': [
                    {'packNum': 0, 'pickNum': 3, 'pick': [0], 'booster': ['c4', 'c5', 'c6']},
                ]
            },
            'user4': {
                'userName': 'Player4',
                'seatNum': 4,
                'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c10', 'c11', 'c12', 'c13', 'c14', 'c15']},
                ]
            },
            'user5': {
                'userName': 'Player5',
                'seatNum': 5,
                'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c11', 'c12', 'c13', 'c14', 'c15']},
                ]
            },
        },
        'carddata': {
            'c1': {'name': 'Card1'}, 'c2': {'name': 'Card2'}, 'c3': {'name': 'Card3'},
            'c4': {'name': 'Card4'}, 'c5': {'name': 'Card5'}, 'c6': {'name': 'Card6'},
            'c10': {'name': 'Card10'}, 'c11': {'name': 'Card11'}, 'c12': {'name': 'Card12'},
            'c13': {'name': 'Card13'}, 'c14': {'name': 'Card14'}, 'c15': {'name': 'Card15'},
        }
    }


def create_draft_with_db_seating():
    """
    Create mock draft data with DB-based seating (team_a/team_b).
    This simulates how real drafts are set up with Discord users mapped to seats.
    """
    draft_data = {
        'sessionID': 'TEST_DB_SEAT_SESSION',
        'users': {
            'dm_user0': {
                'userName': 'Alice',
                'picks': [
                    {'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1', 'c2', 'c3', 'c4', 'c5', 'c6']},
                ]
            },
            'dm_user1': {
                'userName': 'Bob',
                'picks': [
                    {'packNum': 0, 'pickNum': 1, 'pick': [0], 'booster': ['c2', 'c3', 'c4', 'c5', 'c6']},
                ]
            },
            'dm_user2': {
                'userName': 'Carol',
                'picks': [
                    {'packNum': 0, 'pickNum': 2, 'pick': [0], 'booster': ['c3', 'c4', 'c5', 'c6']},
                ]
            },
            'dm_user3': {
                'userName': 'Dave',
                'picks': [
                    {'packNum': 0, 'pickNum': 3, 'pick': [0], 'booster': ['c4', 'c5', 'c6']},
                ]
            },
        },
        'carddata': {
            'c1': {'name': 'Card1'}, 'c2': {'name': 'Card2'}, 'c3': {'name': 'Card3'},
            'c4': {'name': 'Card4'}, 'c5': {'name': 'Card5'}, 'c6': {'name': 'Card6'},
        }
    }

    # Create mock DraftSession with team assignments
    # Team A gets seats 0, 2; Team B gets seats 1, 3
    mock_session = DraftSession(
        id=1,
        session_id='test_session',
        session_type='random',
        team_a=['discord_alice', 'discord_carol'],
        team_b=['discord_bob', 'discord_dave'],
        sign_ups={
            'discord_alice': 'Alice',
            'discord_bob': 'Bob',
            'discord_carol': 'Carol',
            'discord_dave': 'Dave',
        }
    )

    return draft_data, mock_session


class TestTracePackWithStartingSeat:
    """Tests for trace_pack with starting_seat parameter"""

    def test_trace_pack_with_specific_starting_seat(self):
        """Should trace pack starting from specific seat when starting_seat is provided"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        # Trace from seat 0 - should get Player0 -> Player1 -> Player2 -> Player3
        trace = analysis.trace_pack(pack_num=0, length=4, starting_seat=0)

        assert trace is not None
        assert len(trace.picks) == 4
        assert trace.picks[0].user_name == 'Player0'
        assert trace.picks[1].user_name == 'Player1'
        assert trace.picks[2].user_name == 'Player2'
        assert trace.picks[3].user_name == 'Player3'

    def test_trace_pack_with_different_starting_seat(self):
        """Should trace different pack when starting from different seat"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        # Trace from seat 4 - should get Player4 -> Player5 (different pack)
        trace = analysis.trace_pack(pack_num=0, length=2, starting_seat=4)

        assert trace is not None
        assert len(trace.picks) == 2
        assert trace.picks[0].user_name == 'Player4'
        assert trace.picks[1].user_name == 'Player5'

    def test_trace_pack_invalid_starting_seat_falls_back_to_booster_matching(self):
        """Should fall back to booster matching when seat-based trace fails"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        # Seat 2 doesn't have a pack starting at pick_num=0
        # When seat-based tracing fails, it falls back to booster matching
        # which can still find a valid trace
        trace = analysis.trace_pack(pack_num=0, length=4, starting_seat=2)

        # Should still find a trace via booster matching fallback
        # The trace found might not start at seat 2
        assert trace is not None
        assert len(trace.picks) == 4

    def test_trace_pack_without_starting_seat_finds_any_valid(self):
        """Should find any valid trace when starting_seat is not provided"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        # Without starting_seat, should find some valid trace
        trace = analysis.trace_pack(pack_num=0, length=4)

        assert trace is not None
        assert len(trace.picks) == 4


class TestGetValidStartingSeats:
    """Tests for get_valid_starting_seats method"""

    def test_get_valid_starting_seats_returns_list(self):
        """Should return list of valid seat numbers"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        valid_seats = analysis.get_valid_starting_seats(pack_num=0, length=4)

        assert isinstance(valid_seats, list)

    def test_get_valid_starting_seats_includes_seat_0(self):
        """Should include seat 0 which has a complete 4-pick trace"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        valid_seats = analysis.get_valid_starting_seats(pack_num=0, length=4)

        assert 0 in valid_seats

    def test_get_valid_starting_seats_excludes_invalid_seats(self):
        """Should exclude seats that don't produce complete traces"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        valid_seats = analysis.get_valid_starting_seats(pack_num=0, length=4)

        # Seat 2 starts at pick_num=2, so can't produce a 4-pick trace starting at pick 0
        assert 2 not in valid_seats
        assert 3 not in valid_seats

    def test_get_valid_starting_seats_with_different_length(self):
        """Should return different seats based on required trace length"""
        draft_data = create_6_player_draft_data()
        analysis = DraftAnalysis(draft_data)

        # With length 2, more seats might be valid
        valid_seats_2 = analysis.get_valid_starting_seats(pack_num=0, length=2)
        valid_seats_4 = analysis.get_valid_starting_seats(pack_num=0, length=4)

        # Shorter traces should have >= valid seats
        assert len(valid_seats_2) >= len(valid_seats_4)

    def test_get_valid_starting_seats_empty_without_seating(self):
        """Should return empty list when draft has no seating info"""
        # Draft data without seatNum fields
        draft_data = {
            'sessionID': 'TEST_NO_SEATS',
            'users': {
                'user1': {
                    'userName': 'Alice',
                    'picks': [{'packNum': 0, 'pickNum': 0, 'pick': [0], 'booster': ['c1', 'c2']}]
                },
            },
            'carddata': {'c1': {'name': 'Card1'}, 'c2': {'name': 'Card2'}}
        }
        analysis = DraftAnalysis(draft_data)

        valid_seats = analysis.get_valid_starting_seats(pack_num=0, length=1)

        assert valid_seats == []


class TestSeatBasedTracingWithDBSeating:
    """Tests for seat-based tracing when seats come from DB (team_a/team_b)"""

    def test_trace_pack_with_db_assigned_seats(self):
        """Should work with seats assigned from team_a/team_b in DraftSession"""
        draft_data, mock_session = create_draft_with_db_seating()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        # With DB seating: Alice(0), Bob(1), Carol(2), Dave(3)
        trace = analysis.trace_pack(pack_num=0, length=4, starting_seat=0)

        assert trace is not None
        assert len(trace.picks) == 4
        assert trace.picks[0].user_name == 'Alice'  # Seat 0
        assert trace.picks[1].user_name == 'Bob'    # Seat 1
        assert trace.picks[2].user_name == 'Carol'  # Seat 2
        assert trace.picks[3].user_name == 'Dave'   # Seat 3

    def test_get_valid_starting_seats_with_db_seating(self):
        """Should return valid seats when using DB-based seating"""
        draft_data, mock_session = create_draft_with_db_seating()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        valid_seats = analysis.get_valid_starting_seats(pack_num=0, length=4)

        assert 0 in valid_seats

    def test_has_seating_true_with_db_seats(self):
        """Should report has_seating=True when seats assigned from DB"""
        draft_data, mock_session = create_draft_with_db_seating()
        analysis = DraftAnalysis(draft_data, draft_session=mock_session)

        assert analysis.has_seating is True


class TestPackTracerDirectly:
    """Direct tests on PackTracer class"""

    def test_pack_tracer_trace_by_seats_with_starting_seat(self):
        """PackTracer._trace_by_seats should respect starting_seat parameter"""
        draft_data = create_6_player_draft_data()
        indexer = DraftIndexer(draft_data)
        tracer = PackTracer(indexer)

        # Direct call to _trace_by_seats
        chain = tracer._trace_by_seats(pack_num=0, length=4, starting_seat=0)

        assert chain is not None
        assert len(chain) == 4
        assert chain[0].user_name == 'Player0'

    def test_pack_tracer_get_valid_starting_seats(self):
        """PackTracer.get_valid_starting_seats should return valid seat list"""
        draft_data = create_6_player_draft_data()
        indexer = DraftIndexer(draft_data)
        tracer = PackTracer(indexer)

        valid_seats = tracer.get_valid_starting_seats(pack_num=0, length=4)

        assert isinstance(valid_seats, list)
        assert 0 in valid_seats


class TestQuizSessionModel:
    """Tests for QuizSession model starting_seat column"""

    def test_quiz_session_has_starting_seat_attribute(self):
        """QuizSession model should have starting_seat attribute"""
        from models import QuizSession

        # Check that the column exists on the model
        assert hasattr(QuizSession, 'starting_seat')

    def test_quiz_session_starting_seat_nullable(self):
        """starting_seat should be nullable for backwards compatibility"""
        from models import QuizSession

        # Create instance without starting_seat
        quiz = QuizSession(
            quiz_id='test-123',
            display_id=1,
            guild_id='guild123',
            channel_id='channel123',
            draft_session_id='draft123',
            pack_trace_data={'picks': []},
            correct_answers=[],
            posted_by='user123'
        )

        # starting_seat should default to None
        assert quiz.starting_seat is None

    def test_quiz_session_with_starting_seat(self):
        """Should be able to set starting_seat on QuizSession"""
        from models import QuizSession

        quiz = QuizSession(
            quiz_id='test-456',
            display_id=2,
            guild_id='guild123',
            channel_id='channel123',
            draft_session_id='draft123',
            starting_seat=3,
            pack_trace_data={'picks': []},
            correct_answers=[],
            posted_by='user123'
        )

        assert quiz.starting_seat == 3
