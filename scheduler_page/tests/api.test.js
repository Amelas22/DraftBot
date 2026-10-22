import test from "node:test";
import assert from "node:assert/strict";
import { loadLink, getJson, putJson, LinkExpired, NotFound } from "../js/api.js";

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
  await assert.rejects(loadLink("#abc", fake));
});

test("loadLink refuses a higher version", async () => {
  const fake = async () => reply(200, { ...LINK, version: 2 });
  await assert.rejects(loadLink("#abc", fake));
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
