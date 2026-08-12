"""
Unit tests for the prize-pool split logic in services.tournament_escrow_service.

compute_allocations had no direct coverage before top25pct was added; the
static-structure tests here pin the pre-existing behavior alongside the new
field-size-dependent structure.
"""
from services.tournament_escrow_service import (
    PAYOUT_CHOICES,
    PAYOUT_STRUCTURES,
    compute_allocations,
    describe_structure,
)


def ranked(n):
    """A finish-ordered field of n teams: 1st first."""
    return [(f"captain{i}", f"Team{i}") for i in range(1, n + 1)]


class TestStaticStructures:
    def test_winner_take_all_pays_everything_to_first(self):
        allocs = compute_allocations(1200, "winner_take_all", ranked(8))
        assert allocs == [(1, "captain1", "Team1", 1200)]

    def test_top4_splits_by_percentages(self):
        allocs = compute_allocations(1000, "top4", ranked(8))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 400), (2, 300), (3, 200), (4, 100)]

    def test_fewer_teams_than_places_renormalizes(self):
        # top4 over a 3-team field: 40/30/20 renormalized over 90.
        allocs = compute_allocations(900, "top4", ranked(3))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 400), (2, 300), (3, 200)]

    def test_rounding_remainder_goes_to_first(self):
        # top2 65/35 of 101: floors to 65/35, remainder 1 -> 1st.
        allocs = compute_allocations(101, "top2", ranked(4))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 66), (2, 35)]
        assert sum(amt for _, _, _, amt in allocs) == 101

    def test_unknown_structure_falls_back_to_winner_take_all(self):
        allocs = compute_allocations(500, "no_such_structure", ranked(4))
        assert allocs == [(1, "captain1", "Team1", 500)]

    def test_empty_pool_or_field_pays_nothing(self):
        assert compute_allocations(0, "top2", ranked(4)) == []
        assert compute_allocations(500, "top2", []) == []


class TestTop25Pct:
    """The league's advertised split: top 25% of teams (rounded down, min one
    place) on a stepped share ladder — 5/3/2/2 for the first four places,
    2 shares for places 5-6, 1 share for every place beyond."""

    def test_sixteen_teams_matches_the_league_page_example(self):
        # 16 teams * 150 tix = 2400 pool; 4 places on 5/3/2/2 shares of 12.
        allocs = compute_allocations(2400, "top25pct", ranked(16))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 1000), (2, 600), (3, 400), (4, 400)]

    def test_eight_teams_pays_two_places(self):
        allocs = compute_allocations(1200, "top25pct", ranked(8))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 750), (2, 450)]

    def test_twelve_teams_pays_three_places(self):
        allocs = compute_allocations(1000, "top25pct", ranked(12))
        assert [(p, amt) for p, _, _, amt in allocs] == [(1, 500), (2, 300), (3, 200)]

    def test_twenty_teams_extends_to_five_places_at_two_shares(self):
        allocs = compute_allocations(1400, "top25pct", ranked(20))
        assert [(p, amt) for p, _, _, amt in allocs] == [
            (1, 500), (2, 300), (3, 200), (4, 200), (5, 200),
        ]

    def test_places_beyond_six_step_down_to_one_share(self):
        # 32 teams -> 8 places on 5/3/2/2/2/2/1/1 (18 shares) of a 4800 pool.
        allocs = compute_allocations(4800, "top25pct", ranked(32))
        assert [(p, amt) for p, _, _, amt in allocs] == [
            (1, 1336), (2, 800), (3, 533), (4, 533),
            (5, 533), (6, 533), (7, 266), (8, 266),
        ]

    def test_tiny_field_still_pays_the_winner(self):
        # 3 teams: 25% rounds down to 0 -> min one place.
        allocs = compute_allocations(450, "top25pct", ranked(3))
        assert allocs == [(1, "captain1", "Team1", 450)]

    def test_whole_pool_is_always_distributed(self):
        for teams in (3, 8, 11, 16, 20, 33):
            for pool in (997, 1400, 2400):
                allocs = compute_allocations(pool, "top25pct", ranked(teams))
                assert sum(amt for _, _, _, amt in allocs) == pool, (teams, pool)

    def test_places_scale_with_field_size(self):
        for teams, places in ((4, 1), (7, 1), (8, 2), (15, 3), (16, 4), (24, 6)):
            allocs = compute_allocations(10_000, "top25pct", ranked(teams))
            assert len(allocs) == places, teams


class TestStructureWiring:
    def test_top25pct_is_offered_alongside_the_static_structures(self):
        assert set(PAYOUT_CHOICES) == set(PAYOUT_STRUCTURES) | {"top25pct"}

    def test_command_choices_use_the_full_list(self):
        from cogs.tournament_commands import TournamentCog

        for command_name in ("create", "payout"):
            command = next(
                c for c in TournamentCog.tournament.walk_commands() if c.name == command_name
            )
            option = next(o for o in command.options if o.name in ("payout", "structure"))
            assert set(c if isinstance(c, str) else c.value for c in option.choices) == set(
                PAYOUT_CHOICES
            ), command_name

    def test_describe_top25pct(self):
        text = describe_structure("top25pct")
        assert "25%" in text and "5/3/2/2" in text

    def test_describe_static_structures_unchanged(self):
        assert describe_structure("winner_take_all") == "winner-take-all"
        assert describe_structure("top3") == "top 3 (50/30/20)"
