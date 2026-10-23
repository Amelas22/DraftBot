import test from "node:test";
import assert from "node:assert/strict";
import { createApi, createFixtureApi, apiFromHash, checkDoc, LinkExpired, NeedsUpdate, NEEDS_UPDATE } from "../js/api.js";

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
