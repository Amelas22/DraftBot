"""Fuzzy matching + in-place linking of premade drafts to tournament matches.

No discord imports — keep this unit-testable against a plain SQLite session.
Shared by the live nudge (tournament_nudge.py) and the retro-link CLI
(scripts/link_premade_to_tournament.py).
"""
import difflib
from dataclasses import dataclass

from sqlalchemy import select

from models.draft_session import DraftSession
from models.tournament import (
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
)

MATCH_THRESHOLD = 0.45


def _name_score(name, team_name):
    """0..1 similarity of a draft team name to a participant name.

    Substring containment is forced to >= 0.90 so abbreviations like
    'strixhaven' confidently resolve to 'Strixhaven Dropouts'.
    """
    norm = (name or "").strip().lower()
    target = (team_name or "").lower()
    if not norm or not target:
        return 0.0
    score = difflib.SequenceMatcher(None, norm, target).ratio()
    if norm in target:
        score = max(score, 0.9)
    return score


@dataclass(frozen=True)
class CandidateLink:
    match_id: int
    reversed: bool
    confidence: float
    a_name: str          # match participant A name (tournament order)
    b_name: str          # match participant B name (tournament order)
    round_number: int


async def resolve_candidate_matches(session, tournament, team_a_name, team_b_name):
    """Candidate matches the draft (team_a_name vs team_b_name) might be.

    Candidates are non-bye, unplayed, and not already linked to another draft.
    Each is scored by the better of the two name orientations; only matches
    whose weaker name clears MATCH_THRESHOLD are returned, sorted by confidence.
    """
    parts = (await session.execute(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament.id)
    )).scalars().all()
    by_id = {p.id: p for p in parts}

    rows = (await session.execute(
        select(TournamentMatch, TournamentRound.round_number)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(
            TournamentRound.tournament_id == tournament.id,
            TournamentMatch.is_bye.is_(False),
            TournamentMatch.team_a_wins.is_(None),
        )
    )).all()

    linked_ids = set((await session.execute(
        select(DraftSession.tournament_match_id)
        .where(DraftSession.tournament_match_id.isnot(None))
    )).scalars().all())

    candidates = []
    for match, round_number in rows:
        if match.id in linked_ids:
            continue
        pa = by_id.get(match.team_a_participant_id)
        pb = by_id.get(match.team_b_participant_id)
        if pa is None or pb is None or pa.id == pb.id:
            continue
        normal = min(_name_score(team_a_name, pa.team_name),
                     _name_score(team_b_name, pb.team_name))
        flipped = min(_name_score(team_a_name, pb.team_name),
                      _name_score(team_b_name, pa.team_name))
        if flipped > normal:
            confidence, is_reversed = flipped, True
        else:
            confidence, is_reversed = normal, False
        if confidence >= MATCH_THRESHOLD:
            candidates.append(CandidateLink(
                match_id=match.id, reversed=is_reversed, confidence=confidence,
                a_name=pa.team_name, b_name=pb.team_name, round_number=round_number))
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates
