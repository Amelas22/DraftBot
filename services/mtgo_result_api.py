"""
In-process HTTP endpoint for the Windows MTGO worker to report match results.

Runs on the bot's own event loop (same process), so it can drive the Discord-dependent
result cascade directly. DISABLED unless MTGO_API_TOKEN is set in the environment, so
merging this file changes nothing until you opt in.

Intended deployment: bind on the droplet's Tailscale interface and require a bearer token;
the Windows worker reaches it over Tailscale. Nothing here moves assets — the endpoint only
records who won a draft match — so the security surface is low (a leaked token could post a
bogus result, which an admin can override).

Endpoints (all require  Authorization: Bearer <MTGO_API_TOKEN> ):
  GET  /health
  GET  /pairings/active[?sessionId=...]
       -> {pairings: [{sessionId, matchNumber, playerA, playerB, discordA, discordB,
           sessionType}]}  — pending pairings whose BOTH players have linked MTGO
       accounts; the worker uses these to know which player-pairs to watch for.
  POST /matches/report
       body: {playerA, playerB, winner, gamesWinner, gamesLoser, [sessionId], [mtgoMatchId]}
       playerA/playerB/winner are MTGO usernames; winner must equal playerA or playerB.

Env:
  MTGO_API_TOKEN   shared bearer token (required to enable)
  MTGO_API_HOST    bind host (default 0.0.0.0; keep it behind Tailscale/firewall)
  MTGO_API_PORT    bind port (default 8787)
"""
import os

from loguru import logger

try:
    from aiohttp import web  # bundled with py-cord
except Exception:  # pragma: no cover
    web = None

from services.mtgo_result_service import report_mtgo_match, pending_pairings


def _authorized(request) -> bool:
    token = os.getenv("MTGO_API_TOKEN")
    return bool(token) and request.headers.get("Authorization", "") == f"Bearer {token}"


async def start_result_api(bot):
    """Start the result-reporting HTTP server if MTGO_API_TOKEN is set. No-op otherwise."""
    token = os.getenv("MTGO_API_TOKEN")
    if not token:
        return  # not configured -> stay off
    if web is None:
        logger.warning("[mtgo-api] MTGO_API_TOKEN set but aiohttp is unavailable; not starting")
        return

    host = os.getenv("MTGO_API_HOST", "0.0.0.0")
    port = int(os.getenv("MTGO_API_PORT", "8787"))

    async def health(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True})

    async def pairings(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.query.get("sessionId")
        try:
            rows = await pending_pairings(session_id=session_id)
        except Exception as e:
            logger.exception(f"[mtgo-api] pairings failed: {e}")
            return web.json_response({"error": "internal error"}, status=500)
        return web.json_response({"pairings": rows})

    async def report(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        missing = [k for k in ("playerA", "playerB", "winner", "gamesWinner", "gamesLoser") if k not in body]
        if missing:
            return web.json_response({"error": f"missing fields: {', '.join(missing)}"}, status=400)

        try:
            status, detail = await report_mtgo_match(
                bot,
                player_a=str(body["playerA"]),
                player_b=str(body["playerB"]),
                winner=str(body["winner"]),
                games_winner=int(body["gamesWinner"]),
                games_loser=int(body["gamesLoser"]),
                session_id=body.get("sessionId"),
                mtgo_match_id=body.get("mtgoMatchId"),
            )
        except Exception as e:
            logger.exception(f"[mtgo-api] report failed: {e}")
            return web.json_response({"error": "internal error"}, status=500)

        # ok -> 200; no unreported pairing -> 409; mapping/validation issue -> 422
        http_status = 200 if status == "ok" else (409 if status == "no_match" else 422)
        return web.json_response({"status": status, "detail": detail}, status=http_status)

    app = web.Application()
    app.add_routes([
        web.get("/health", health),
        web.get("/pairings/active", pairings),
        web.post("/matches/report", report),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[mtgo-api] result-reporting server listening on {host}:{port}")
