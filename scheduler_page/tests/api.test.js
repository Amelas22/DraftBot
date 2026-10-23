import test from "node:test";
import assert from "node:assert/strict";
import { loadLink, getJson, putJson, checkDoc, LinkExpired, NotFound, BrokenLink, NeedsUpdate, NEEDS_UPDATE } from "../js/api.js";

const LINK = {
  format: "draftbot.scheduler.link", version: 1, view: "usual", user: "42",
  exp: "2026-10-29T12:00:00Z",
  urls: { usual_get: "https://x/get", usual_put: "https://x/put" },
};
const reply = (status, body) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => body,
});

test("loadLink reads file 8", async () => {
  const seen = [];
  const fake = async (url, opts) => { seen.push([url, opts]); return reply(200, LINK); };
  const link = await loadLink("#abc", fake);
  assert.equal(seen[0][0], "link/abc.json");
  assert.equal(link.urls.usual_put, "https://x/put");
  assert.equal(link.view, "usual");
  assert.equal(link.fixture, false);
});

test("loadLink refuses unknown format", async () => {
  const fake = async () => reply(200, { ...LINK, format: "something.else" });
  await assert.rejects(loadLink("#abc", fake), NeedsUpdate);
});

test("loadLink refuses a higher version", async () => {
  const fake = async () => reply(200, { ...LINK, version: 2 });
  await assert.rejects(loadLink("#abc", fake), NeedsUpdate);
});

test("checkDoc passes a known version-1 file and refuses anything else as NeedsUpdate", () => {
  const doc = { format: "f", version: 1 };
  assert.equal(checkDoc(doc, "f"), doc);
  for (const bad of [null, { format: "g", version: 1 }, { format: "f", version: 2 }, { format: "f" }, { format: "f", version: "1" }]) {
    assert.throws(() => checkDoc(bad, "f"), (e) => e instanceof NeedsUpdate && e.message === NEEDS_UPDATE);
  }
});

test("loadLink turns an unknown token (404) into LinkExpired", async () => {
  await assert.rejects(loadLink("#abc", async () => reply(404, {})), LinkExpired);
});

test("loadLink with no token is an error", async () => {
  await assert.rejects(loadLink("", async () => reply(200, LINK)));
});

test("loadLink fixture mode never has a put url", async () => {
  const link = await loadLink("#fixture=usual-week", async () => { throw new Error("no fetch"); });
  assert.equal(link.fixture, true);
  assert.equal(link.view, "usual");
  assert.equal(link.urls.usual_get, "fixtures/usual-week.json");
  assert.equal(link.urls.usual_put, null);
});

test("test_expired_link_message: getJson 403 rejects with LinkExpired", async () => {
  await assert.rejects(getJson("https://x/get", async () => reply(403, {})), LinkExpired);
});

test("getJson 404 rejects with NotFound and uses no-store", async () => {
  let opts;
  await assert.rejects(getJson("https://x/get", async (u, o) => { opts = o; return reply(404, {}); }), NotFound);
  assert.equal(opts.cache, "no-store");
});

test("putJson sends application/json", async () => {
  let call;
  const fake = async (url, opts) => { call = [url, opts]; return reply(200, {}); };
  await putJson("https://x/put", { a: 1 }, fake);
  assert.equal(call[1].method, "PUT");
  assert.equal(call[1].headers["Content-Type"], "application/json");
  assert.equal(call[1].body, '{"a":1}');
});

test("putJson 403 rejects with LinkExpired", async () => {
  await assert.rejects(putJson("https://x/put", {}, async () => reply(403, {})), LinkExpired);
});

test("putJson refuses a null url", async () => {
  await assert.rejects(putJson(null, {}, async () => reply(200, {})));
});

test("loadLink: a usual link without usual_get is broken", async () => {
  const { BrokenLink } = await import("../js/api.js");
  const bad = { ...LINK, urls: { usual_put: "https://x/put" } };
  await assert.rejects(loadLink("#abc", async () => reply(200, bad)), BrokenLink);
});

test("fixture=usual-week stays the usual view", async () => {
  const link = await loadLink("#fixture=usual-week");
  assert.equal(link.view, "usual");
  assert.deepEqual(link.urls, { usual_get: "fixtures/usual-week.json", usual_put: null });
});

test("fixture=lfg routes to the find-a-team view as Ben", async () => {
  const link = await loadLink("#fixture=lfg");
  assert.equal(link.view, "lfg");
  assert.equal(link.fixture, true);
  assert.equal(link.user, "900000000000000002");
  assert.deepEqual(link.urls, {
    lfg_get: "fixtures/lfg.json", usual_get: "fixtures/usual-week.json", usual_put: null,
  });
});

test("fixture=match.good routes to the match view with no uploads", async () => {
  const link = await loadLink("#fixture=match.good");
  assert.equal(link.view, "match");
  assert.equal(link.fixture, true);
  assert.equal(link.user, "900000000000000002");
  assert.deepEqual(link.urls, {
    shared_get: "fixtures/match-shared.good.json", me_get: "fixtures/match-me.good.json",
    week_put: null, request_put: null,
    usual_get: "fixtures/usual-week.json", usual_put: null,
  });
});

test("fixture=match. with no scenario is an error", async () => {
  await assert.rejects(loadLink("#fixture=match."));
  await assert.rejects(loadLink("#fixture=match"));
});

const real = (view, urls) => async () => reply(200, { ...LINK, view, urls });

test("a real lfg link needs lfg_get", async () => {
  await assert.rejects(loadLink("#abc", real("lfg", { usual_get: "https://x/g" })), BrokenLink);
  const link = await loadLink("#abc", real("lfg", { lfg_get: "https://x/l", usual_get: "https://x/g" }));
  assert.equal(link.view, "lfg");
});

test("a real match link needs shared_get and me_get", async () => {
  await assert.rejects(loadLink("#abc", real("match", { shared_get: "https://x/s" })), BrokenLink);
  await assert.rejects(loadLink("#abc", real("match", { me_get: "https://x/m" })), BrokenLink);
  const link = await loadLink("#abc", real("match", { shared_get: "https://x/s", me_get: "https://x/m" }));
  assert.equal(link.view, "match");
});
