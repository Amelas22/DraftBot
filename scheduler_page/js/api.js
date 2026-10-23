// JSON access to the scheduler files in Spaces. No DOM; fetch is injectable for tests.
export class LinkExpired extends Error {
  constructor(message = "link expired") { super(message); this.name = "LinkExpired"; }
}
export class BrokenLink extends Error {
  constructor(message = "link file is incomplete") { super(message); this.name = "BrokenLink"; }
}
export const NEEDS_UPDATE = "This page needs updating.";
// A file in a format or version this page doesn't know: the page itself is out of date.
export class NeedsUpdate extends Error {
  constructor(message = NEEDS_UPDATE) { super(message); this.name = "NeedsUpdate"; }
}
export class NotFound extends Error {
  constructor(message = "not found") { super(message); this.name = "NotFound"; }
}

const LINK_FORMAT = "draftbot.scheduler.link";

// Every file the page reads names its format and a version; this page reads version 1.
export function checkDoc(body, format) {
  if (!body || body.format !== format || !Number.isInteger(body.version) || body.version > 1) throw new NeedsUpdate();
  return body;
}
const FIXTURE_RE = /^[A-Za-z0-9._-]+$/;

const FIXTURE_VIEWER = "900000000000000002"; // Ben, the viewer in every lfg and match fixture
const FIXTURE_USUAL = "fixtures/usual-week.json";

function fixtureLink(name) {
  const base = { exp: null, fixture: true };
  if (name === "lfg") {
    return { ...base, view: "lfg", user: FIXTURE_VIEWER,
      urls: { lfg_get: "fixtures/lfg.json", usual_get: FIXTURE_USUAL, usual_put: null } };
  }
  if (name === "match" || name.startsWith("match.")) {
    const scenario = name.slice("match.".length);
    if (!scenario) throw new Error("match fixture needs a scenario");
    return { ...base, view: "match", user: FIXTURE_VIEWER,
      urls: {
        shared_get: `fixtures/match-shared.${scenario}.json`, me_get: `fixtures/match-me.${scenario}.json`,
        week_put: null, request_put: null, usual_get: FIXTURE_USUAL, usual_put: null,
      } };
  }
  return { ...base, view: "usual", user: "fixture",
    urls: { usual_get: `fixtures/${name}.json`, usual_put: null } };
}

const REQUIRED_URLS = { usual: ["usual_get"], lfg: ["lfg_get"], match: ["shared_get", "me_get", "request_put"] };

// Page-relative on purpose: the page lives at .../scheduler/index.html, so "link/x.json" resolves beside it.
export async function loadLink(hash, fetchFn = fetch) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) throw new Error("no link token");

  if (raw.startsWith("fixture=")) {
    const name = raw.slice("fixture=".length);
    if (!FIXTURE_RE.test(name)) throw new Error("bad fixture name");
    return fixtureLink(name);
  }

  if (!/^[A-Za-z0-9_-]+$/.test(raw)) throw new Error("bad link token");
  const res = await fetchFn(`link/${raw}.json`, { cache: "no-store" });
  if (res.status === 403 || res.status === 404) throw new LinkExpired();
  if (!res.ok) throw new Error(`link file: HTTP ${res.status}`);
  const body = await res.json();
  checkDoc(body, LINK_FORMAT);
  const urls = body.urls || {};
  for (const key of REQUIRED_URLS[body.view] || []) {
    if (typeof urls[key] !== "string" || !urls[key]) throw new BrokenLink(`link file has no ${key}`);
  }
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
