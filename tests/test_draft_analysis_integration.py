"""
Tests for DraftAnalysis class - quiz-agnostic draft parsing and pack tracing.
"""

import pytest
import asyncio
from services.draft_analysis import DraftAnalysis
from services.draft_data_loader import load_from_spaces


@pytest.mark.asyncio
async def test_draft_analysis_with_real_draft():
    """Test DraftAnalysis with a real draft from Spaces."""

    # Recent July 2025 team draft
    spaces_key = "team/PowerLSV-1752150873653-DB38WEXI13.json"

    print(f"\n{'='*70}")
    print(f"Testing DraftAnalysis with: {spaces_key}")
    print(f"{'='*70}\n")

    # Use new API: load directly from Spaces
    analysis = await DraftAnalysis.from_spaces(spaces_key)
    assert analysis is not None, "Failed to load draft data"

    print(f"✅ Draft loaded using new API")
    print(f"   Draftmancer Session ID: {analysis.session_id}")
    print(f"   Number of players: {analysis.num_players}")
    print(f"   Has seating: {analysis.has_seating}")
    print("")

    # Show pack statistics
    print(f"Pack statistics:")
    for pack_num in [0, 1, 2]:
        picks = analysis.get_picks_for_pack(pack_num)
        print(f"   Pack {pack_num}: {len(picks)} picks")
    print("")

    # Test pack tracing for each pack (new API)
    print("Testing trace_pack for each pack:")
    print("-" * 70)

    for pack_num in [0, 1, 2]:
        print(f"\nPack {pack_num}:")

        trace = analysis.trace_pack(pack_num, length=4)

        if trace:
            print(f"  ✅ Found 4-pick sequence!")
            for i, pick in enumerate(trace.picks):
                card = analysis.get_card(pick.picked_id)
                print(f"     Pick {i+1}: {pick.user_name} (pickNum {pick.pick_num}, "
                      f"{len(pick.booster_ids)} cards) → {card.name}")
        else:
            print(f"  ❌ No 4-pick sequence found")

            # Debug why
            pack_picks = analysis.get_picks_for_pack(pack_num)
            print(f"  Total picks in pack: {len(pack_picks)}")

            # Show first few picks to understand structure
            print(f"  First 5 picks:")
            for i, pick in enumerate(pack_picks[:5]):
                card = analysis.get_card(pick.picked_id)
                print(f"     {i+1}. {pick.user_name} pick# {pick.pick_num}: "
                      f"{len(pick.booster_ids)} cards → {card.name}")

    print("")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    # Run directly for debugging
    asyncio.run(test_draft_analysis_with_real_draft())
