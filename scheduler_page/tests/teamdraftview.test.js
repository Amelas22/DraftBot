import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LinkExpired } from "../js/api.js";
import { mountTeamDraft } from "../js/teamdraftview.js";

const doc = JSON.parse(readFileSync(new URL("../fixtures/team-draft.json", import.meta.url)));
// Before the fixture window opens, so every cell can still be booked.
const now = () => new Date("2026-10-25T00:00:00Z");

// A just-enough DOM: elements keep their children, attributes and listeners so a test can find a button and click it.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.className = ""; this.textContent = ""; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get classList() { return { add() {}, remove() {} }; }
  find(test) { return test(this) ? this : this.children.map((c) => c.find?.(test)).find(Boolean); }
  click() { for (const fn of this.listeners.click || []) fn({}); }
}

let saved;
before(() => {
  saved = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = {
    createElement: () => new Node(), visibilityState: "hidden", addEventListener() {}, removeEventListener() {},
  };
});
after(() => {
  if (saved) Object.defineProperty(globalThis, "document", saved);
  else delete globalThis.document;
});

const fakeApi = (calls, get) => ({
  getUsual: async () => ({}),
  get: async (path) => { calls.push(["get", path]); return get(path); },
  post: async (path, body) => { calls.push(["post", path, body]); return { ok: true, state: doc }; },
});
const mount = async (api, deps) => {
  const root = new Node();
  const view = await mountTeamDraft(root, api, { now, ...deps });
  return { root, view };
};

const MATCH = { get: "/api/tournament-match", actions: "/api/tournament-match/actions" };

test("mounting with no paths fetches the team-draft matchup", async () => {
  const calls = [];
  await mount(fakeApi(calls, () => { throw new LinkExpired(); }));
  assert.deepEqual(calls, [["get", "/api/team-draft"]]);
});

test("mounting with paths fetches the matchup they name", async () => {
  const calls = [];
  await mount(fakeApi(calls, () => { throw new LinkExpired(); }), { paths: MATCH });
  assert.deepEqual(calls, [["get", "/api/tournament-match"]]);
});

// Picks cell 100 and presses Book; resolves with the calls the view made.
async function book(deps) {
  const calls = [];
  const { root, view } = await mount(fakeApi(calls, () => doc), deps);
  root.find((n) => n.attrs["data-j"] === "100").click();
  root.find((n) => n.textContent === "Book this time").click();
  await new Promise((r) => setImmediate(r));
  view.dispose();
  return calls;
}

test("booking with no paths posts to the team-draft actions", async () => {
  const post = (await book()).find((c) => c[0] === "post");
  assert.equal(post[1], "/api/team-draft/actions");
  assert.equal(post[2].kind, "book");
});

test("booking with paths posts to the actions they name", async () => {
  const post = (await book({ paths: MATCH })).find((c) => c[0] === "post");
  assert.equal(post[1], "/api/tournament-match/actions");
  assert.equal(post[2].kind, "book");
});
