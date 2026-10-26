import test from "node:test";
import assert from "node:assert/strict";
import { createApi, createFixtureApi, apiFromHash, checkDoc, LinkExpired, NeedsUpdate, NotEnabled, NEEDS_UPDATE } from "../js/api.js";

const TOKEN = "abcdefghijklmnopqrstuv"; // 22 chars, like a real link token
const reply = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const recorder = (status, body) => {
  const calls = [];
  const fn = async (url, opts) => { calls.push([url, opts]); return reply(status, body); };
  fn.calls = calls;
  return fn;
};

test("every call sends the bearer token and no-store", async () => {
  const fake = recorder(200, { ok: 1 });
  const api = createApi(TOKEN, fake);
  await api.me();
  await api.getUsual();
  await api.putUsual({ a: 1 });
  await api.get("/api/x");
  await api.post("/api/y", { b: 2 });
  assert.equal(fake.calls.length, 5);
  for (const [, opts] of fake.calls) {
    assert.equal(opts.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(opts.cache, "no-store");
  }
  assert.deepEqual(fake.calls.map(([u, o]) => [o.method || "GET", u]), [
    ["GET", "/api/me"], ["GET", "/api/usual-week"], ["PUT", "/api/usual-week"], ["GET", "/api/x"], ["POST", "/api/y"],
  ]);
  const put = fake.calls[2][1];
  assert.equal(put.headers["Content-Type"], "application/json");
  assert.equal(put.body, '{"a":1}');
});

test("a 401 is LinkExpired on every call", async () => {
  const api = createApi(TOKEN, recorder(401, { error: "expired" }));
  for (const call of [() => api.me(), () => api.getUsual(), () => api.putUsual({}), () => api.get("/a"), () => api.post("/a", {})]) {
    await assert.rejects(call(), LinkExpired);
  }
});

test("a 403 not_enabled is NotEnabled on every call; another 403 keeps its code", async () => {
  const api = createApi(TOKEN, recorder(403, { error: "not_enabled" }));
  for (const call of [() => api.me(), () => api.getUsual(), () => api.putUsual({}), () => api.get("/a"), () => api.post("/a", {})]) {
    await assert.rejects(call(), NotEnabled);
  }
  const other = createApi(TOKEN, recorder(403, { error: "wrong_view" }));
  await assert.rejects(other.get("/a"), (e) => !(e instanceof NotEnabled) && e.code === "wrong_view" && e.status === 403);
});

test("getUsual turns 404 into null", async () => {
  const api = createApi(TOKEN, recorder(404, { error: "not_set" }));
  assert.equal(await api.getUsual(), null);
});

test("other failures carry the server's error code and status", async () => {
  for (const [status, code] of [[400, "bad_input"], [413, "too_large"], [429, "rate_limited"], [500, "boom"]]) {
    const api = createApi(TOKEN, recorder(status, { error: code }));
    await assert.rejects(api.putUsual({}), (e) => e instanceof Error && e.code === code && e.status === status);
  }
  const noBody = createApi(TOKEN, async () => ({ ok: false, status: 502, json: async () => { throw new Error("html"); } }));
  await assert.rejects(noBody.me(), (e) => e.status === 502 && typeof e.code === "string");
});

test("a 404 on anything but getUsual is an error", async () => {
  const api = createApi(TOKEN, recorder(404, { error: "nope" }));
  await assert.rejects(api.me(), (e) => e.status === 404);
});

test("apiFromHash with a token builds the real api", async () => {
  const fake = recorder(200, { view: "usual" });
  const { api, fixture } = apiFromHash(`#${TOKEN}`, fake);
  assert.equal(fixture, false);
  await api.me();
  assert.equal(fake.calls[0][1].headers.Authorization, `Bearer ${TOKEN}`);
});

test("apiFromHash refuses a bad token before any fetch", () => {
  const never = async () => { throw new Error("fetched"); };
  for (const h of ["", "#", "#short", "#" + "a".repeat(65), "#abc def ghijklmnopqrstu", "#../../etc/passwd/aaaaaaaa", "#fixture="]) {
    assert.throws(() => apiFromHash(h, never), (e) => e.message !== "fetched", h);
  }
});

test("apiFromHash fixture names are checked", () => {
  assert.throws(() => apiFromHash("#fixture=../secret", async () => { throw new Error("fetched"); }));
  assert.throws(() => apiFromHash("#fixture=a/b"));
});

test("a fixture api reads fixtures and never writes", async () => {
  const fake = recorder(200, { format: "f", version: 1 });
  const { api, fixture } = apiFromHash("#fixture=usual-week", fake);
  assert.equal(fixture, true);
  assert.deepEqual(await api.me(), { user: "fixture", guild: "fixture", view: "usual", exp: null });
  assert.equal(fake.calls.length, 0);
  assert.deepEqual(await api.getUsual(), { format: "f", version: 1 });
  assert.equal(fake.calls[0][0], "fixtures/usual-week.json");
  const doc = { format: "x" };
  assert.equal(await api.putUsual(doc), doc);
  assert.equal(fake.calls.length, 1);
});

test("a missing fixture file reads as not set", async () => {
  const api = createFixtureApi("nothing", recorder(404, {}));
  assert.equal(await api.getUsual(), null);
});

test("checkDoc passes a known version-1 file and refuses anything else as NeedsUpdate", () => {
  const doc = { format: "f", version: 1 };
  assert.equal(checkDoc(doc, "f"), doc);
  for (const bad of [null, { format: "g", version: 1 }, { format: "f", version: 2 }, { format: "f" }, { format: "f", version: "1" }]) {
    assert.throws(() => checkDoc(bad, "f"), (e) => e instanceof NeedsUpdate && e.message === NEEDS_UPDATE);
  }
});

// Serves the real fixture files from disk, and records what was asked for.
import { readFileSync } from "node:fs";
const diskFetch = () => {
  const calls = [];
  const fn = async (url) => {
    calls.push(url);
    return reply(200, JSON.parse(readFileSync(new URL(`../${url}`, import.meta.url))));
  };
  fn.calls = calls;
  return fn;
};
const uuid = () => crypto.randomUUID();

test("the teams fixture is an lfg link with a saved week and the teams list", async () => {
  const fake = diskFetch();
  const api = createFixtureApi("teams", fake);
  const me = await api.me();
  assert.equal(me.view, "lfg");
  assert.equal(me.user, "900000000000000002");
  assert.equal((await api.getUsual()).format, "draftbot.scheduler.usual_week");
  const state = await api.get("/api/teams");
  assert.equal(state.format, "draftbot.scheduler.teams");
  assert.equal(state.teams.length, 3);
  assert.deepEqual(fake.calls, ["fixtures/usual-week.json", "fixtures/teams.json"]);
});

test("the teams fixture fakes actions in memory and never fetches for them", async () => {
  const fake = diskFetch();
  const api = createFixtureApi("teams", fake);
  await api.get("/api/teams");
  const before = fake.calls.length;

  let r = await api.post("/api/teams/actions", { action_id: uuid(), kind: "request_join", team_id: "101", note: "hi" });
  assert.equal(r.ok, true);
  assert.deepEqual(r.state.me.outgoing, [{ team_id: "101", status: "pending", note: "hi" }]);

  r = await api.post("/api/teams/actions", { action_id: uuid(), kind: "withdraw_request", team_id: "101" });
  assert.equal(r.ok, true);
  assert.equal(r.state.me.outgoing[0].status, "withdrawn");

  r = await api.post("/api/teams/actions", { action_id: uuid(), kind: "withdraw_request", team_id: "101" });
  assert.deepEqual([r.ok, r.code], [false, "request_gone"]);

  r = await api.post("/api/teams/actions", { action_id: uuid(), kind: "open_team", size: 4, note: "new" });
  assert.equal(r.ok, true);
  const mine = r.state.teams.at(-1);
  assert.equal(mine.captain, "900000000000000002");
  assert.equal(mine.size, 4);
  assert.equal(mine.members[0].user, "900000000000000002");
  assert.ok(mine.members[0].cells);
  assert.deepEqual(r.state.me.on_teams, [mine.team_id]);

  r = await api.post("/api/teams/actions", { action_id: uuid(), kind: "request_join", team_id: mine.team_id, note: "" });
  assert.deepEqual([r.ok, r.code], [false, "already_member"]);

  assert.equal(fake.calls.length, before);
  assert.deepEqual((await api.get("/api/teams")).me.on_teams, [mine.team_id]);
});

const ROOK = "900000000000000011";
const BEN = "900000000000000002";
const KIT = "900000000000000016";
const act = (api, body) => api.post("/api/teams/actions", { action_id: uuid(), ...body });

test("the teams-captain fixture is Rook, captain of a team with two requests, and Rook's week", async () => {
  const fake = diskFetch();
  const api = createFixtureApi("teams-captain", fake);
  const me = await api.me();
  assert.deepEqual([me.view, me.user], ["lfg", ROOK]);
  const usual = await api.getUsual();
  const state = await api.get("/api/teams");
  const rook = state.teams.find((t) => t.team_id === "101").members.find((m) => m.user === ROOK);
  assert.deepEqual([usual.format, usual.version, usual.zone, usual.cells], ["draftbot.scheduler.usual_week", 1, rook.zone, rook.cells]);
  assert.deepEqual(state.me.on_teams, ["101"]);
  assert.deepEqual(state.me.incoming["101"].map((r) => r.name).sort(), ["Ben", "Kit"]);
  assert.deepEqual(fake.calls, ["fixtures/teams-captain.json"]);
});

test("the captain fixture accepts to full, and the other requests are filled", async () => {
  const api = createFixtureApi("teams-captain", diskFetch());
  await api.get("/api/teams");
  let r = await act(api, { kind: "accept", team_id: "102", requester: BEN });
  assert.deepEqual([r.ok, r.code], [false, "not_captain"]);
  r = await act(api, { kind: "accept", team_id: "101", requester: "nobody" });
  assert.deepEqual([r.ok, r.code], [false, "request_gone"]);
  r = await act(api, { kind: "accept", team_id: "101", requester: BEN });
  assert.equal(r.ok, true);
  const t = r.state.teams.find((x) => x.team_id === "101");
  assert.deepEqual(t.members.map((m) => m.name), ["Rook", "Wren", "Ben"]);
  assert.ok(t.members[2].cells);
  assert.equal(t.status, "full");
  assert.equal(r.state.me.incoming["101"], undefined);
  r = await act(api, { kind: "accept", team_id: "101", requester: KIT });
  assert.deepEqual([r.ok, r.code], [false, "team_full"]);
});

test("the captain fixture declines, pauses and closes", async () => {
  const api = createFixtureApi("teams-captain", diskFetch());
  let r = await act(api, { kind: "decline", team_id: "101", requester: KIT });
  assert.equal(r.ok, true);
  assert.deepEqual(r.state.me.incoming["101"].map((x) => x.name), ["Ben"]);
  r = await act(api, { kind: "decline", team_id: "101", requester: KIT });
  assert.deepEqual([r.ok, r.code], [false, "request_gone"]);
  r = await act(api, { kind: "set_taking_requests", team_id: "101", on: false });
  assert.equal(r.state.teams.find((x) => x.team_id === "101").taking_requests, false);
  r = await act(api, { kind: "close_team", team_id: "102" });
  assert.deepEqual([r.ok, r.code], [false, "not_captain"]);
  r = await act(api, { kind: "close_team", team_id: "101" });
  assert.equal(r.ok, true);
  assert.equal(r.state.teams.some((x) => x.team_id === "101"), false);
  assert.deepEqual(r.state.me.on_teams, []);
  assert.equal(r.state.me.incoming["101"], undefined);
  r = await act(api, { kind: "close_team", team_id: "101" });
  assert.deepEqual([r.ok, r.code], [false, "team_closed"]);
});

test("leaving a team: a member leaves and a full team reopens; the captain leaving closes it", async () => {
  // The same file seen by Wren, a plain member of Rook's team, after it filled.
  const asWren = async (url) => {
    const doc = JSON.parse(readFileSync(new URL(`../${url}`, import.meta.url)));
    doc.me = { user: "900000000000000012", on_teams: ["101"], outgoing: [], incoming: {} };
    doc.teams[0].status = "full";
    return reply(200, doc);
  };
  const wren = createFixtureApi("teams-captain", asWren);
  assert.equal((await wren.me()).user, "900000000000000012");
  let r = await act(wren, { kind: "leave_team", team_id: "102" });
  assert.deepEqual([r.ok, r.code], [false, "bad_input"]);
  r = await act(wren, { kind: "leave_team", team_id: "101" });
  assert.equal(r.ok, true);
  const t = r.state.teams.find((x) => x.team_id === "101");
  assert.deepEqual([t.members.map((m) => m.name), t.status, t.taking_requests], [["Rook"], "open", true]);
  assert.deepEqual(r.state.me.on_teams, []);

  const rook = createFixtureApi("teams-captain", diskFetch());
  r = await act(rook, { kind: "leave_team", team_id: "101" });
  assert.equal(r.ok, true);
  assert.equal(r.state.teams.some((x) => x.team_id === "101"), false);
});

const TD = "/api/team-draft/actions";
const td = (api, body) => api.post(TD, { action_id: uuid(), ...body });

test("the team-draft fixture is a team_draft link with the viewer's week and an open matchup", async () => {
  const fake = diskFetch();
  const api = createFixtureApi("team-draft", fake);
  const me = await api.me();
  assert.equal(me.view, "team_draft");
  assert.equal(me.user, "900000000000000002");
  assert.equal((await api.getUsual()).format, "draftbot.scheduler.usual_week");
  const state = await api.get("/api/team-draft");
  assert.equal(state.format, "draftbot.scheduler.team_draft");
  assert.equal(state.sides.length, 2);
  assert.equal(state.booking.start_utc, null);
});

test("the team-draft fixture's window starts at the current half hour, so cells can be booked", async () => {
  const api = createFixtureApi("team-draft", diskFetch());
  const { window } = await api.get("/api/team-draft");
  const start = Date.parse(window.start_utc);
  assert.equal(start % 1800000, 0);
  assert.ok(Math.abs(start - Date.now()) <= 1800000);
});

test("the team-draft fixture books, moves and clears in memory, bumping the version", async () => {
  const fake = diskFetch();
  const api = createFixtureApi("team-draft", fake);
  const { window } = await api.get("/api/team-draft");
  const at = (j) => new Date(Date.parse(window.start_utc) + j * 1800000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const before = fake.calls.length;

  let r = await td(api, { kind: "book", start_utc: at(100), seen_version: 0 });
  assert.deepEqual([r.ok, r.code, r.state.booking.start_utc, r.state.booking.version, r.state.booking.by_name], [true, null, at(100), 1, "Ben"]);
  r = await td(api, { kind: "book", start_utc: at(101), seen_version: 0 });
  assert.deepEqual([r.ok, r.code, r.state.booking.start_utc, r.state.booking.version], [false, "changed", at(100), 1]);
  r = await td(api, { kind: "book", start_utc: at(101), seen_version: 1 });
  assert.deepEqual([r.ok, r.state.booking.start_utc, r.state.booking.version], [true, at(101), 2]);
  r = await td(api, { kind: "book", start_utc: at(-1), seen_version: 2 });
  assert.deepEqual([r.ok, r.code], [false, "past"]);
  r = await td(api, { kind: "book", start_utc: at(336), seen_version: 2 });
  assert.deepEqual([r.ok, r.code], [false, "out_of_window"]);
  r = await td(api, { kind: "clear", seen_version: 2 });
  assert.deepEqual([r.ok, r.state.booking.start_utc, r.state.booking.version], [true, null, 3]);
  r = await td(api, { kind: "clear", seen_version: 3 });
  assert.deepEqual([r.ok, r.code], [false, "not_booked"]);
  assert.equal((await api.get("/api/team-draft")).booking.version, 3);
  assert.equal(fake.calls.length, before);
});
