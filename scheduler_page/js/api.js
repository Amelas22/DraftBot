// The page's only door to the bot: a JSON API on the same origin, authorised by the link token. No DOM;
// fetch is injectable for tests.
export class LinkExpired extends Error {
  constructor(message = "link expired") { super(message); this.name = "LinkExpired"; }
}
// The server's scheduler feature flag is off for the link's guild.
export class NotEnabled extends Error {
  constructor(message = "not enabled") { super(message); this.name = "NotEnabled"; this.code = "not_enabled"; this.status = 403; }
}
export const NEEDS_UPDATE = "This page needs updating.";
// A document in a format or version this page doesn't know: the page itself is out of date.
export class NeedsUpdate extends Error {
  constructor(message = NEEDS_UPDATE) { super(message); this.name = "NeedsUpdate"; }
}

// Every document the page reads names its format and a version; this page reads version 1.
export function checkDoc(body, format) {
  if (!body || body.format !== format || !Number.isInteger(body.version) || body.version > 1) throw new NeedsUpdate();
  return body;
}

const FIXTURE_RE = /^[A-Za-z0-9._-]+$/;
const TOKEN_RE = /^[A-Za-z0-9_-]{20,64}$/;
const USUAL_PATH = "/api/usual-week";

// A failed call: `code` is the server's error code ("too_large", "rate_limited", ...), `status` the HTTP status.
async function failure(res) {
  let code = `http_${res.status}`;
  try {
    const body = await res.json();
    if (body && typeof body.error === "string") code = body.error;
  } catch { /* not JSON: keep the status-based code */ }
  return Object.assign(new Error(`${code} (HTTP ${res.status})`), { code, status: res.status });
}

export function createApi(token, fetchFn = fetch) {
  async function call(method, path, body) {
    const headers = { Authorization: `Bearer ${token}` };
    const opts = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetchFn(path, opts);
    if (res.status === 401) throw new LinkExpired();
    if (res.status === 403) {
      const err = await failure(res);
      throw err.code === "not_enabled" ? new NotEnabled() : err;
    }
    return res;
  }
  async function json(method, path, body) {
    const res = await call(method, path, body);
    if (!res.ok) throw await failure(res);
    return res.json();
  }
  return {
    fixture: false,
    me: () => json("GET", "/api/me"),
    async getUsual() {
      const res = await call("GET", USUAL_PATH);
      if (res.status === 404) return null;
      if (!res.ok) throw await failure(res);
      return res.json();
    },
    putUsual: (doc) => json("PUT", USUAL_PATH, doc),
    get: (path) => json("GET", path),
    post: (path, body) => json("POST", path, body),
  };
}

// The same shape, served from fixtures/*.json. It never writes and never needs a token.
export function createFixtureApi(name, fetchFn = fetch) {
  if (!FIXTURE_RE.test(name)) throw new Error("bad fixture name");
  const read = async (file) => {
    const res = await fetchFn(file, { cache: "no-store" });
    if (res.status === 404) return null;
    if (!res.ok) throw await failure(res);
    return res.json();
  };
  if (Object.hasOwn(TEAMS_FIXTURES, name)) return teamsFixtureApi(read, TEAMS_FIXTURES[name]);
  const refuse = () => { throw new Error("fixtures are read only"); };
  return {
    fixture: true,
    me: async () => ({ user: "fixture", guild: "fixture", view: "usual", exp: null }),
    getUsual: () => read(`fixtures/${name}.json`),
    putUsual: async (doc) => doc,
    get: (path) => read(`fixtures/${path.replace(/^\/+/, "")}.json`),
    post: refuse,
  };
}

// `#fixture=teams`: Ben (who has a usual week) on a find-a-team link. `#fixture=teams-captain`: Rook, captain of
// a team with two requests, whose usual week is his own member row. Actions change an in-memory copy of the
// file and answer like the server, so the whole flow can be clicked through without a bot. The viewer is the
// file's `me.user`.
const TEAMS_FIXTURES = {
  teams: { file: "fixtures/teams.json", usualFile: "fixtures/usual-week.json", name: "Ben" },
  "teams-captain": { file: "fixtures/teams-captain.json", usualFile: null, name: "Rook" },
};

function teamsFixtureApi(read, { file, usualFile, name }) {
  let state = null;
  let week = null;
  // The member row of the viewer, as a usual-week document.
  const ownWeek = () => {
    const row = state.teams.flatMap((t) => t.members).find((m) => m.user === state.me.user);
    return row?.cells ? { format: "draftbot.scheduler.usual_week", version: 1, zone: row.zone, cells: row.cells, saved_at: "2026-10-18T22:14:09Z" } : null;
  };
  // Every file is read before any action, so an action never fetches.
  const load = async () => {
    if (usualFile) week ??= read(usualFile);
    await week;
    state ??= await read(file);
    week ??= Promise.resolve(ownWeek());
    return state;
  };
  const refused = (code) => ({ ok: false, code, state });
  const me = () => state.me;
  const user = () => state.me.user;
  const incoming = (teamId) => me().incoming[teamId] ?? [];
  const dropIncoming = (teamId, who) => {
    const left = incoming(teamId).filter((r) => r.user !== who);
    if (left.length) me().incoming[teamId] = left;
    else delete me().incoming[teamId];
  };
  const dropTeam = (team) => {
    state.teams = state.teams.filter((t) => t !== team);
    delete me().incoming[team.team_id];
    me().on_teams = me().on_teams.filter((id) => id !== team.team_id);
  };

  async function act(body) {
    await load();
    const team = state.teams.find((t) => t.team_id === body.team_id);
    const captains = team && team.captain === user();
    const pending = () => me().outgoing.find((r) => r.team_id === body.team_id && r.status === "pending");
    const full = () => team.status === "full" || team.members.length >= team.size;
    if (body.kind === "open_team") {
      const doc = await week;
      const id = String(Math.max(0, ...state.teams.map((t) => Number(t.team_id))) + 1);
      state.teams.push({
        team_id: id, captain: user(), captain_name: name, size: body.size,
        note: body.note || "", taking_requests: true, status: "open",
        members: [{ user: user(), name, zone: doc?.zone ?? null, cells: doc?.cells ?? null }],
      });
      me().on_teams.push(id);
    } else if (body.kind === "request_join") {
      if (!team) return refused("team_closed");
      if (team.members.some((m) => m.user === user())) return refused("already_member");
      if (full()) return refused("team_full");
      if (!team.taking_requests) return refused("not_taking_requests");
      if (!pending()) {
        me().outgoing = me().outgoing.filter((r) => r.team_id !== body.team_id);
        me().outgoing.push({ team_id: body.team_id, status: "pending", note: body.note || "" });
      }
    } else if (body.kind === "withdraw_request") {
      const r = pending();
      if (!r) return refused("request_gone");
      r.status = "withdrawn";
    } else if (body.kind === "set_taking_requests") {
      if (!captains) return refused("not_captain");
      team.taking_requests = body.on;
    } else if (body.kind === "accept" || body.kind === "decline") {
      if (!captains) return refused("not_captain");
      if (body.kind === "accept" && full()) return refused("team_full");
      const r = incoming(body.team_id).find((x) => x.user === body.requester);
      if (!r) return refused("request_gone");
      dropIncoming(body.team_id, r.user);
      if (body.kind === "accept") {
        team.members.push({ user: r.user, name: r.name, zone: r.zone, cells: r.cells });
        if (full()) { team.status = "full"; delete me().incoming[team.team_id]; }
      }
    } else if (body.kind === "close_team" || body.kind === "leave_team") {
      if (!team) return refused(body.kind === "close_team" ? "team_closed" : "bad_input");
      if (body.kind === "close_team" && !captains) return refused("not_captain");
      if (captains) {
        dropTeam(team);
      } else {
        if (!team.members.some((m) => m.user === user())) return refused("bad_input");
        team.members = team.members.filter((m) => m.user !== user());
        if (team.status === "full") { team.status = "open"; team.taking_requests = true; }
        me().on_teams = me().on_teams.filter((id) => id !== team.team_id);
      }
    } else {
      return refused("bad_input");
    }
    return { ok: true, code: null, state };
  }

  return {
    fixture: true,
    me: async () => { await load(); return { user: user(), guild: "fixture", view: "lfg", exp: null }; },
    getUsual: async () => { await load(); return structuredClone(await week); },
    putUsual: async (doc) => doc,
    get: async (path) => {
      if (path !== "/api/teams") throw new Error(`no fixture for ${path}`);
      return structuredClone(await load());
    },
    post: async (path, body) => {
      if (path !== "/api/teams/actions") throw new Error(`no fixture for ${path}`);
      return structuredClone(await act(body));
    },
  };
}

// `#fixture=<name>` -> the fixture api; `#<token>` -> the real one. Anything else throws before any fetch.
export function apiFromHash(hash, fetchFn = fetch) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) throw new Error("no link token");
  if (raw.startsWith("fixture=")) return { api: createFixtureApi(raw.slice("fixture=".length), fetchFn), fixture: true };
  if (!TOKEN_RE.test(raw)) throw new Error("bad link token");
  return { api: createApi(raw, fetchFn), fixture: false };
}
