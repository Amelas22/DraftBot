// The page's only door to the bot: a JSON API on the same origin, authorised by the link token. No DOM;
// fetch is injectable for tests.
export class LinkExpired extends Error {
  constructor(message = "link expired") { super(message); this.name = "LinkExpired"; }
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

// `#fixture=<name>` -> the fixture api; `#<token>` -> the real one. Anything else throws before any fetch.
export function apiFromHash(hash, fetchFn = fetch) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) throw new Error("no link token");
  if (raw.startsWith("fixture=")) return { api: createFixtureApi(raw.slice("fixture=".length), fetchFn), fixture: true };
  if (!TOKEN_RE.test(raw)) throw new Error("bad link token");
  return { api: createApi(raw, fetchFn), fixture: false };
}
