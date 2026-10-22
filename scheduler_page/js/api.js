// JSON access to the scheduler files in Spaces. No DOM; fetch is injectable for tests.
export class LinkExpired extends Error {
  constructor(message = "link expired") { super(message); this.name = "LinkExpired"; }
}
export class BrokenLink extends Error {
  constructor(message = "link file is incomplete") { super(message); this.name = "BrokenLink"; }
}
export class NotFound extends Error {
  constructor(message = "not found") { super(message); this.name = "NotFound"; }
}

const LINK_FORMAT = "draftbot.scheduler.link";
const FIXTURE_RE = /^[A-Za-z0-9._-]+$/;

// Page-relative on purpose: the page lives at .../scheduler/index.html, so "link/x.json" resolves beside it.
export async function loadLink(hash, fetchFn = fetch) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) throw new Error("no link token");

  if (raw.startsWith("fixture=")) {
    const name = raw.slice("fixture=".length);
    if (!FIXTURE_RE.test(name)) throw new Error("bad fixture name");
    return {
      view: "usual", user: "fixture", exp: null, fixture: true,
      urls: { usual_get: `fixtures/${name}.json`, usual_put: null },
    };
  }

  if (!/^[A-Za-z0-9_-]+$/.test(raw)) throw new Error("bad link token");
  const res = await fetchFn(`link/${raw}.json`, { cache: "no-store" });
  if (res.status === 403 || res.status === 404) throw new LinkExpired();
  if (!res.ok) throw new Error(`link file: HTTP ${res.status}`);
  const body = await res.json();
  if (!body || body.format !== LINK_FORMAT) throw new Error("unknown link format");
  if (!Number.isInteger(body.version) || body.version > 1) throw new Error("link file is newer than this page");
  const urls = body.urls || {};
  if (body.view === "usual" && (typeof urls.usual_get !== "string" || !urls.usual_get)) throw new BrokenLink("link file has no usual_get");
  return { view: body.view, user: body.user, exp: body.exp, urls, fixture: false };
}

export async function getJson(url, fetchFn = fetch) {
  const res = await fetchFn(url, { cache: "no-store" });
  if (res.status === 403) throw new LinkExpired();
  if (res.status === 404) throw new NotFound(url);
  if (!res.ok) throw new Error(`GET failed: HTTP ${res.status}`);
  return res.json();
}

export async function putJson(url, body, fetchFn = fetch) {
  if (!url) throw new Error("no upload url");
  const res = await fetchFn(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 403) throw new LinkExpired();
  if (!res.ok) throw new Error(`PUT failed: HTTP ${res.status}`);
}
