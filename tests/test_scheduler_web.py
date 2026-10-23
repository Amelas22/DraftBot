import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from services.availability import MAX_FILE_BYTES, UsualWeek, cells_to_bytes, load_usual_week, save_usual_week
from services.scheduler_links import issue_link
from services.scheduler_web import build_app, client_ip

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


def _doc(zone: str = "America/New_York", cells: tuple[int, ...] = (1, 2, 3)) -> dict:
    return {
        "format": "draftbot.scheduler.usual_week",
        "version": 1,
        "zone": zone,
        "cells": base64.b64encode(cells_to_bytes(cells)).decode("ascii"),
    }


@pytest_asyncio.fixture
async def client(test_db, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>x</title>")
    (static / "style.css").write_text("body{}")
    (static / "js").mkdir()
    (static / "js" / "app.js").write_text("1")
    (static / "tests").mkdir()
    (static / "tests" / "api.test.js").write_text("secret")
    (static / "fixtures").mkdir()
    (static / "fixtures" / "a.json").write_text("{}")
    (static / "fixtures" / "make_fixtures.py").write_text("secret")
    clock = {"now": NOW}
    app = build_app(now=lambda: clock["now"], static_dir=static)
    async with TestClient(TestServer(app)) as c:
        c.clock = clock  # type: ignore[attr-defined]
        yield c


async def _auth(view: str = "usual") -> dict[str, str]:
    url = await issue_link("111", "222", view, NOW)
    return {"Authorization": "Bearer " + url.split("#")[1]}


@pytest.mark.asyncio
async def test_me_requires_token(client):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        resp = await client.get("/api/me", headers=headers)
        assert resp.status == 401
        assert await resp.json() == {"error": "expired"}
    headers = await _auth()
    client.clock["now"] = NOW + timedelta(days=8)
    resp = await client.get("/api/me", headers=headers)
    assert resp.status == 401
    assert await resp.json() == {"error": "expired"}


@pytest.mark.asyncio
async def test_me_returns_identity(client):
    resp = await client.get("/api/me", headers=await _auth("teams"))
    assert resp.status == 200
    body = await resp.json()
    assert body == {"user": "111", "guild": "222", "view": "teams", "exp": "2026-10-29T12:00:00Z"}


@pytest.mark.asyncio
async def test_put_then_get_usual_week(client):
    headers = await _auth()
    resp = await client.get("/api/usual-week", headers=headers)
    assert resp.status == 404
    assert await resp.json() == {"error": "not_set"}
    doc = _doc()
    resp = await client.put("/api/usual-week", data=json.dumps(doc), headers=headers)
    assert resp.status == 200
    assert await resp.json() == doc
    resp = await client.get("/api/usual-week", headers=headers)
    assert resp.status == 200
    assert await resp.json() == doc


@pytest.mark.asyncio
async def test_put_rejects_bad_week(client):
    headers = await _auth()
    await save_usual_week("111", UsualWeek("UTC", frozenset({5})), NOW)
    resp = await client.put("/api/usual-week", data=json.dumps(_doc(zone="Mars/Base")), headers=headers)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_input"}
    resp = await client.put("/api/usual-week", data=b"not json", headers=headers)
    assert resp.status == 400
    big = json.dumps(_doc()) + " " * (MAX_FILE_BYTES + 1)
    resp = await client.put("/api/usual-week", data=big, headers=headers)
    assert resp.status == 413
    assert await resp.json() == {"error": "too_large"}
    # Chunked body with no Content-Length cannot bypass the cap either.
    async def chunks():
        yield b" " * (MAX_FILE_BYTES + 10)

    resp = await client.put("/api/usual-week", data=chunks(), headers=headers)
    assert resp.status == 413
    assert await load_usual_week("111") == UsualWeek("UTC", frozenset({5}))


@pytest.mark.asyncio
async def test_static_serves_index_not_tests(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert (await client.get("/index.html")).status == 200
    css = await client.get("/style.css")
    assert css.headers["Content-Type"].startswith("text/css")
    assert (await client.get("/js/app.js")).headers["Content-Type"].startswith("text/javascript")
    assert (await client.get("/fixtures/a.json")).headers["Content-Type"].startswith("application/json")
    for path in (
        "/tests/api.test.js",
        "/fixtures/make_fixtures.py",
        "/js/",
        "/js/missing.js",
        "/fixtures/..%2findex.json",
        "/.env",
    ):
        assert (await client.get(path)).status == 404, path


@pytest.mark.asyncio
async def test_rate_limit(client):
    headers = await _auth()
    for _ in range(60):
        assert (await client.get("/api/me", headers=headers)).status == 200
    resp = await client.get("/api/me", headers=headers)
    assert resp.status == 429
    assert await resp.json() == {"error": "rate_limited"}
    client.clock["now"] = NOW + timedelta(seconds=61)
    assert (await client.get("/api/me", headers=headers)).status == 200


@pytest.mark.asyncio
async def test_ip_rate_limit_uses_forwarded_for_from_loopback(client):
    # Many distinct tokens from one forwarded client hit the per-IP cap.
    statuses = []
    for i in range(121):
        resp = await client.get(
            "/api/me",
            headers={"Authorization": f"Bearer t{i}", "X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
        )
        statuses.append(resp.status)
    assert statuses[:120] == [401] * 120
    assert statuses[120] == 429
    other = await client.get(
        "/api/me", headers={"Authorization": "Bearer x", "X-Forwarded-For": "8.8.8.8"}
    )
    assert other.status == 401


def test_client_ip_trusts_only_last_hop_from_loopback():
    assert client_ip("127.0.0.1", "6.6.6.6, 1.2.3.4") == "1.2.3.4"
    assert client_ip("::1", "1.2.3.4") == "1.2.3.4"
    assert client_ip("127.0.0.1", "") == "127.0.0.1"
    assert client_ip("10.0.0.5", "6.6.6.6, 1.2.3.4") == "10.0.0.5"


@pytest.mark.asyncio
async def test_forged_leading_xff_does_not_evade_ip_limit(client):
    for i in range(120):
        resp = await client.get(
            "/api/me", headers={"Authorization": "Bearer t", "X-Forwarded-For": f"6.6.6.{i}, 1.2.3.4"}
        )
        assert resp.status == 401
    resp = await client.get(
        "/api/me", headers={"Authorization": "Bearer t", "X-Forwarded-For": "7.7.7.7, 1.2.3.4"}
    )
    assert resp.status == 429


@pytest.mark.asyncio
async def test_put_body_in_two_chunks(client):
    headers = await _auth()
    raw = json.dumps(_doc()).encode()

    async def chunks():
        yield raw[:20]
        await asyncio.sleep(0.05)
        yield raw[20:]

    resp = await client.put("/api/usual-week", data=chunks(), headers=headers)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_oversized_body_in_several_chunks(client):
    headers = await _auth()

    async def chunks():
        for _ in range(5):
            yield b" " * 1000
            await asyncio.sleep(0.02)

    resp = await client.put("/api/usual-week", data=chunks(), headers=headers)
    assert resp.status == 413


@pytest.mark.asyncio
async def test_unknown_routes_404_and_static_has_nosniff(client):
    assert (await client.get("/nothing")).status == 404
    assert (await client.get("/")).headers["X-Content-Type-Options"] == "nosniff"


def test_rate_limiter_is_bounded():
    from services import scheduler_web as sw

    limiter = sw.RateLimiter()
    for i in range(sw.MAX_KEYS + 50):
        limiter.over_limit(f"k{i}", 5, NOW)
    assert len(limiter._hits) <= sw.MAX_KEYS
