import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { paintGrid, weekColumns, applyStroke, toFile, loadWeek, saveFailedMessage, actionButtons, autosaver, mountUsual } from "../js/usual.js";
import { decodeCells } from "../js/week.js";
import { NeedsUpdate } from "../js/api.js";

const cell = (day, hour, half = 0) => day * 48 + hour * 2 + half;

test("applyStroke paints a rectangle", () => {
  const out = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  assert.equal(out.size, 12);
  assert.ok(out.has(cell(2, 19, 1)));
  assert.ok(!out.has(cell(0, 19)));
});

test("applyStroke works whichever corner it starts from", () => {
  assert.equal(applyStroke(new Set(), cell(3, 20, 1), cell(1, 19), true).size, 12);
});

test("applyStroke erases and does not mutate its input", () => {
  const start = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  const snapshot = new Set(start);
  assert.equal(applyStroke(start, cell(2, 19), cell(2, 20, 1), false).size, 8);
  assert.deepEqual(start, snapshot);
});

test("applyStroke leaves locked cells as they were", () => {
  assert.deepEqual(applyStroke(new Set([3]), 0, 5, true, undefined, new Set([2, 3])), new Set([0, 1, 3, 4, 5]));
  assert.deepEqual(applyStroke(new Set([0, 1, 2, 3]), 0, 3, false, undefined, new Set([2])), new Set([2]));
});

test("toFile shape", () => {
  const cells = new Set([0, 1, 100, 335]);
  const f = toFile("America/New_York", cells, new Date("2026-10-22T12:34:56.789Z"));
  assert.equal(f.format, "draftbot.scheduler.usual_week");
  assert.equal(f.version, 1);
  assert.equal(f.zone, "America/New_York");
  assert.deepEqual(decodeCells(f.cells), cells);
  assert.equal(f.saved_at, "2026-10-22T12:34:56Z");
});

test("loadWeek treats a never-saved week (null) as empty", async () => {
  const w = await loadWeek({ getUsual: async () => null });
  assert.equal(w.cells.size, 0);
  assert.equal(w.zone, null);
});

test("loadWeek reads a doc and refuses an unknown format", async () => {
  const doc = toFile("Europe/Paris", new Set([5]), new Date());
  const ok = await loadWeek({ getUsual: async () => doc });
  assert.deepEqual(ok.cells, new Set([5]));
  assert.equal(ok.zone, "Europe/Paris");
  await assert.rejects(loadWeek({ getUsual: async () => ({ format: "x", version: 1 }) }), NeedsUpdate);
  await assert.rejects(loadWeek({ getUsual: async () => ({ ...doc, version: 2 }) }), NeedsUpdate);
});

test("save failures say what went wrong", () => {
  const err = (code) => Object.assign(new Error(code), { code });
  assert.equal(saveFailedMessage(err("too_large")), "That week is too big to save.");
  assert.equal(saveFailedMessage(err("rate_limited")), "Too many saves just now. Wait a minute and press Save again.");
  assert.equal(saveFailedMessage(err("not_enabled")), "Find a team is turned off in this server right now.");
  assert.equal(saveFailedMessage(err("bad_input")), "That week couldn't be saved. Reload the page and try again.");
  assert.equal(saveFailedMessage(new TypeError("fetch failed")), "Could not save. Check your connection and press Save again.");
});

test("actionButtons offers Back to teams only when there is somewhere to go back to", () => {
  assert.deepEqual(actionButtons(undefined), ["Save", "Clear"]);
  assert.deepEqual(actionButtons(() => {}), ["Save", "Clear", "Back to teams"]);
});

test("actionButtons names the page the back button returns to", () => {
  assert.deepEqual(actionButtons(() => {}, "Back to the matchup"), ["Save", "Clear", "Back to the matchup"]);
  assert.deepEqual(actionButtons(undefined, "Back to the matchup"), ["Save", "Clear"]);
});


// An autosaver over a recording save; fail makes the next save throw once.
function recorder() {
  const r = { saves: 0, said: [], fail: false, during: null };
  r.auto = autosaver(async () => {
    r.during?.();
    r.during = null;
    if (r.fail) { r.fail = false; throw new TypeError("fetch failed"); }
    r.saves += 1;
  }, (text) => r.said.push(text));
  return r;
}

test("autosaver saves a run of changes once, and nothing when nothing changed", async () => {
  const r = recorder();
  await r.auto.flush();
  assert.equal(r.saves, 0);
  r.auto.touch();
  r.auto.touch();
  await r.auto.flush();
  assert.equal(r.saves, 1);
  assert.deepEqual(r.said, ["Saving…", "Saved."]);
});

test("autosaver keeps a failed change and saves it on the next try", async () => {
  const r = recorder();
  r.fail = true;
  r.auto.touch();
  await r.auto.flush();
  assert.equal(r.saves, 0);
  assert.equal(r.said.at(-1), "Couldn't save just now. Trying again shortly.");
  await r.auto.flush();
  assert.equal(r.saves, 1);
});

test("autosaver keeps saving until nothing is pending, so a flush covers a change made while it ran", async () => {
  const r = recorder();
  r.auto.touch();
  r.during = () => r.auto.touch();
  await r.auto.flush();
  assert.equal(r.saves, 2);
  assert.deepEqual(r.said, ["Saving…", "Saving…", "Saved."]);
});

test("autosaver says why a refused save failed and does not retry it", async () => {
  const said = [];
  let tries = 0;
  const auto = autosaver(async () => { tries += 1; throw Object.assign(new Error("bad"), { code: "bad_input" }); }, (t) => said.push(t));
  auto.touch();
  await auto.flush();
  await auto.flush();
  assert.equal(tries, 1);
  assert.equal(said.at(-1), "That week couldn't be saved. Reload the page and try again.");
});

test("autosaver cancel drops a pending change", async () => {
  const r = recorder();
  r.auto.touch();
  r.auto.cancel();
  await r.auto.flush();
  assert.equal(r.saves, 0);
});

// A just-enough DOM: elements keep their children, attributes, classes and listeners.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.className = ""; this.textContent = ""; this.classes = new Set(); }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this.attrs[k] = v; if (k.startsWith("data-")) this.dataset[k.slice(5)] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get classList() {
    const c = this.classes;
    return { add: (...n) => n.forEach((x) => c.add(x)), remove: (...n) => n.forEach((x) => c.delete(x)), toggle: (n, on) => (on ? c.add(n) : c.delete(n)) };
  }
  closest(sel) { return this.className.split(" ").includes(sel.slice(1)) ? this : null; }
  find(test) { return test(this) ? this : this.children.map((c) => c.find?.(test)).find(Boolean); }
}

let savedDocument;
before(() => {
  savedDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = { createElement: () => new Node() };
});
after(() => {
  if (savedDocument) Object.defineProperty(globalThis, "document", savedDocument);
  else delete globalThis.document;
});

test("on a round view a stroke on one Sunday shows on both", async () => {
  const LA = "America/Los_Angeles";
  const puts = [];
  const api = {
    getUsual: async () => toFile(LA, new Set(), new Date()),
    putUsual: async (doc) => { puts.push(doc); return doc; },
  };
  const root = new Node();
  const editor = await mountUsual(root, api, {
    startUtc: new Date("2026-11-15T08:00:00Z"), cells: 384, shape: "round", zones: [LA], defaultZone: LA,
  });
  const node = (j) => root.find((n) => n.attrs["data-i"] === String(j));
  const grid = root.find((n) => n.attrs.role === "grid");
  for (const fn of grid.listeners.keydown) fn({ target: node(0), key: " ", preventDefault() {} });
  assert.ok(node(0).classes.has("on"));
  assert.ok(!node(336).classes.has("on"));
  await editor.flush();
  assert.equal(puts.length, 1);
  assert.deepEqual(decodeCells(puts[0].cells), new Set([6 * 48]));
  assert.ok(node(0).classes.has("on"));
  assert.ok(node(336).classes.has("on"));
});

test("a save that lands mid-stroke keeps both Sundays: the stroke lands on the redrawn cells", async () => {
  const LA = "America/Los_Angeles";
  const puts = [];
  const api = {
    getUsual: async () => toFile(LA, new Set(), new Date()),
    putUsual: async (doc) => { puts.push(doc); return doc; },
  };
  const root = new Node();
  const editor = await mountUsual(root, api, {
    startUtc: new Date("2026-11-15T08:00:00Z"), cells: 384, shape: "round", zones: [LA], defaultZone: LA,
  });
  const node = (j) => root.find((n) => n.attrs["data-i"] === String(j));
  const grid = root.find((n) => n.attrs.role === "grid");
  for (const fn of grid.listeners.keydown) fn({ target: node(0), key: " ", preventDefault() {} });
  document.elementFromPoint = (x) => node(x);
  try {
    for (const fn of grid.listeners.pointerdown) fn({ button: 0, pointerType: "touch", clientX: 100, pointerId: 1 });
    await editor.flush(); // the save lands and redraws Sunday 22 while the stroke is still down
    assert.ok(node(336).classes.has("on"));
    for (const fn of grid.listeners.pointermove) fn({ clientX: 101 });
    for (const fn of grid.listeners.pointerup) fn({});
  } finally {
    delete document.elementFromPoint;
  }
  await editor.flush();
  assert.ok(node(0).classes.has("on"));
  assert.ok(node(336).classes.has("on"));
  assert.ok(decodeCells(puts.at(-1).cells).has(6 * 48));
});

test("a stroke reports the cells it covered, never locked ones", () => {
  const seen = [];
  const painter = paintGrid({ columns: weekColumns(), gridLabel: "g", cells: new Set(), onChange: (keys) => seen.push(keys), locked: new Set([2]) });
  const node = (j) => painter.grid.find((n) => n.attrs["data-i"] === String(j));
  document.elementFromPoint = (x) => node(x);
  try {
    for (const fn of painter.grid.listeners.pointerdown) fn({ button: 0, pointerType: "touch", clientX: 0, pointerId: 1 });
    for (const fn of painter.grid.listeners.pointermove) fn({ clientX: 4 });
    for (const fn of painter.grid.listeners.pointerup) fn({});
    assert.deepEqual(seen.at(-1), new Set([0, 1, 3, 4]));
    for (const fn of painter.grid.listeners.pointerdown) fn({ button: 0, pointerType: "touch", clientX: 7, pointerId: 1 });
    for (const fn of painter.grid.listeners.pointercancel) fn({});
    assert.deepEqual(seen.at(-1), new Set());
  } finally {
    delete document.elementFromPoint;
  }
  for (const fn of painter.grid.listeners.keydown) fn({ target: node(6), key: " ", preventDefault() {} });
  assert.deepEqual(seen.at(-1), new Set([6]));
});
