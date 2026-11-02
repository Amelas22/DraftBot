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

// On team-draft-new (Fern, no usual week): opens Edit this week, ticks or unticks the also-usual box, paints the
// last day's 12 PM and saves. Resolves with the root and that cell's free count before and after.
// The fixture window starts at the real current half hour, so the view runs on the real clock (a run that spans
// local midnight could pick the wrong day).
async function paintAndSave(alsoUsual) {
  const api = createFixtureApi("team-draft-new", diskFetch);
  const root = new Node();
  const view = await mountTeamDraft(root, api, { now: () => new Date() });
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const when = `${columnLabel(editorDates(new Date(), zone)[6])}, 12:00 PM`;
    const before = freeAt(root, when);

    byText(root, "Edit this week").click();
    await tick();
    root.find((n) => n.attrs.type === "checkbox").checked = alsoUsual;
    const grid = root.find((n) => n.attrs.role === "grid");
    const target = root.find((n) => n.attrs["data-i"] === String(6 * 48 + 24));
    for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
    byText(root, "Save").click();
    await tick();
    return { root, before, after: freeAt(root, when) };
  } finally {
    view.dispose();
  }
}

test("Edit this week saves overrides alone and the heatmap counts them", async () => {
  const { root, before, after } = await paintAndSave(false);
  assert.ok(byText(root, "Saved."));
  assert.equal(after, before + 1);
  // Unticked, no usual week was written.
  assert.ok(byText(root, "Enter my availability"));
});

test("Edit this week with the box ticked also keeps the week as the usual week", async () => {
  const { root, before, after } = await paintAndSave(true);
  assert.ok(byText(root, "Saved."));
  assert.equal(after, before + 1);
  assert.ok(byText(root, "Edit my usual week"));
});

// The readout text; the view sets it as a plain string.
const readoutOf = (root) => root.find((n) => n.attrs.class === "readout" || n.className === "readout");

test("clicking a half-hour writes who is free; hovering shows counts and leaving the grid brings the names back", async () => {
  const { root, view } = await mount(fakeApi([], () => doc));
  try {
    const grid = root.find((n) => n.attrs.role === "grid");
    const cell = (j) => root.find((n) => n.attrs["data-j"] === String(j));
    cell(100).click();
    const names = readoutOf(root).textContent;
    const when = names.split(": ")[0];
    const nobody = "Rook's team — nobody · Ash's team — nobody";
    assert.equal(names, `${when}: ${nobody} (Ben, Rook, Wren, Ash, Pike, Fern, Moss not free)`);
    for (const fn of grid.listeners.pointerover) fn({ type: "pointerover", pointerType: "mouse", target: { closest: () => cell(101) } });
    assert.match(readoutOf(root).textContent, /\d–\d free$/);
    for (const fn of grid.listeners.pointerleave) fn({ pointerType: "mouse" });
    assert.equal(readoutOf(root).textContent, names);
    // Switching a player off recomputes the clicked names.
    root.find((n) => n.attrs["aria-pressed"] && n.children.includes("Ben")).click();
    assert.equal(readoutOf(root).textContent, `${when}: ${nobody} (Rook, Wren, Ash, Pike, Fern, Moss not free)`);
    // With the pointer on a cell, a toggle leaves its counts alone.
    for (const fn of grid.listeners.pointerover) fn({ type: "pointerover", pointerType: "mouse", target: { closest: () => cell(101) } });
    const counts = readoutOf(root).textContent;
    root.find((n) => n.attrs["aria-pressed"] && n.children.includes("Rook")).click();
    assert.equal(readoutOf(root).textContent, counts);
  } finally {
    view.dispose();
  }
});

const chip = (root, name) => root.find((n) => n.attrs["aria-pressed"] && n.children.includes(name));
const over = (grid, cell, extra) => { for (const fn of grid.listeners.pointerover) fn({ type: "pointerover", target: { closest: () => cell }, ...extra }); };

test("after a tap the names follow a toggle even though touch fires pointerover", async () => {
  const { root, view } = await mount(fakeApi([], () => doc));
  try {
    const grid = root.find((n) => n.attrs.role === "grid");
    const cell = root.find((n) => n.attrs["data-j"] === "100");
    over(grid, cell, { pointerType: "touch" });
    cell.click();
    assert.match(readoutOf(root).textContent, /Ben, Rook/);
    chip(root, "Ben").click();
    assert.doesNotMatch(readoutOf(root).textContent, /Ben/);
  } finally {
    view.dispose();
  }
});

test("keyboard focus on a cell shows its counts and does not hold the names back from a toggle", async () => {
  const { root, view } = await mount(fakeApi([], () => doc));
  try {
    const grid = root.find((n) => n.attrs.role === "grid");
    root.find((n) => n.attrs["data-j"] === "100").click();
    const cell = root.find((n) => n.attrs["data-j"] === "101");
    for (const fn of grid.listeners.focusin) fn({ type: "focusin", target: { closest: () => cell } });
    assert.match(readoutOf(root).textContent, /\d–\d free$/);
    chip(root, "Ben").click();
    // Focus is not a hover: the clicked half-hour's names come back.
    assert.match(readoutOf(root).textContent, /Rook's team — nobody/);
    assert.doesNotMatch(readoutOf(root).textContent, /Ben/);
  } finally {
    view.dispose();
  }
});

test("a window slide while hovering does not freeze the readout", async () => {
  let onVisible;
  document.addEventListener = (type, fn) => { if (type === "visibilitychange") onVisible = fn; };
  document.visibilityState = "visible";
  let current = doc;
  const { root, view } = await mount(fakeApi([], () => current));
  try {
    const grid = root.find((n) => n.attrs.role === "grid");
    over(grid, root.find((n) => n.attrs["data-j"] === "101"), { pointerType: "mouse" });
    current = { ...doc, window: { ...doc.window, start_utc: new Date(Date.parse(doc.window.start_utc) + 1800000).toISOString() } };
    onVisible();
    await tick();
    assert.match(readoutOf(root).textContent, /^Hover or tap/);
  } finally {
    view.dispose();
    document.addEventListener = () => {};
    document.visibilityState = "hidden";
  }
});
