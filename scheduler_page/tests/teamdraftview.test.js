import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LinkExpired, createFixtureApi } from "../js/api.js";
import { mountTeamDraft } from "../js/teamdraftview.js";
import { columnLabel, editorDates } from "../js/thisweek.js";

const doc = JSON.parse(readFileSync(new URL("../fixtures/team-draft.json", import.meta.url)));
// Before the fixture window opens, so every cell can still be booked.
const now = () => new Date("2026-10-25T00:00:00Z");

// A just-enough DOM: elements keep their children, attributes and listeners so a test can find a button and click it.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.className = ""; this.textContent = ""; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this.attrs[k] = v; if (k.startsWith("data-")) this.dataset[k.slice(5)] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get classList() { return { add() {}, remove() {}, toggle() {} }; }
  closest(sel) { return this.className.split(" ").includes(sel.slice(1)) ? this : null; }
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

test("only a person with neither a usual week nor an override is marked as having no week", async () => {
  const { root, view } = await mount(fakeApi([], () => doc));
  const marked = [];
  const walk = (n) => {
    if (n.className === "noweek") marked.push(n);
    (n.children || []).forEach(walk);
  };
  walk(root);
  view.dispose();
  // Fern has nothing; Moss has only an override.
  assert.equal(marked.length, 1);
});

// Serves the real fixture files from disk.
const diskFetch = async (url) => {
  try {
    const body = JSON.parse(readFileSync(new URL(`../${url}`, import.meta.url)));
    return { ok: true, status: 200, json: async () => body };
  } catch {
    return { ok: false, status: 404, json: async () => null };
  }
};
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
const byText = (root, text) => root.find((n) => n.textContent === text);
// The free count a heatmap cell's label gives ("Sun 8 Nov, 12:00 PM: 0–1 free").
const freeAt = (root, when) => {
  const n = root.find((x) => x.attrs["aria-label"]?.startsWith(`${when}:`));
  return n.attrs["aria-label"].split(": ")[1].split(" ")[0].split("–").map(Number).reduce((a, b) => a + b, 0);
};

test("Edit this week paints a time, saves and comes back to a heatmap that counts it", async () => {
  const api = createFixtureApi("team-draft-new", diskFetch);
  const root = new Node();
  // The fixture window starts at the real current half hour, so the view runs on the real clock.
  const view = await mountTeamDraft(root, api, { now: () => new Date() });
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const lastDay = editorDates(new Date(), zone)[6];
    const when = `${columnLabel(lastDay)}, 12:00 PM`;
    const before = freeAt(root, when);

    byText(root, "Edit this week").click();
    await tick();
    const grid = root.find((n) => n.attrs.role === "grid");
    const target = root.find((n) => n.attrs["data-i"] === String(6 * 48 + 24));
    for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
    byText(root, "Save").click();
    await tick();

    assert.ok(byText(root, "Saved."));
    assert.equal(freeAt(root, when), before + 1);
  } finally {
    view.dispose();
  }
});
