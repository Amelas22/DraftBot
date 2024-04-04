async def add_match_result(session, match_number, player1_id, player2_id):
    new_match_result = MatchResult(
        session_id=session.id,
        match_number=match_number,
        player1_id=player1_id,
        player2_id=player2_id
    )
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            db_session.add(new_match_result)
            await db_session.commit()
