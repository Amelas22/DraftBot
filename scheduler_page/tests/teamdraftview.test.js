import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LinkExpired, createFixtureApi } from "../js/api.js";
import { mountTeamDraft } from "../js/teamdraftview.js";
import { gridFrame } from "../js/grid.js";
import { windowColumns } from "../js/grid.js";
import { addDays, deadlineOnOrAfter, weekWindow } from "../js/rounds.js";

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
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const byText = (root, text) => root.find((n) => n.textContent === text);
// My availability's part of the pane, which holds the editor while the heatmap is hidden beside it.
const mineBox = (root) => root.find((n) => n.className === "minehost");
// The card's words now showing; the other views' words sit hidden beside them.
const cardTitle = (root) => root.find((n) => n.className === "words" && n.attrs["aria-hidden"] === "false").children[0].textContent;
// The free count a heatmap cell's label gives ("Sun 8 Nov, 12:00 PM: 0–1 free").
const freeAt = (root, when) => {
  const n = root.find((x) => x.attrs["aria-label"]?.startsWith(`${when}:`));
  return n.attrs["aria-label"].split(": ")[1].split(" ")[0].split("–").map(Number).reduce((a, b) => a + b, 0);
};

// The fixture's week (Sunday to Sunday Pacific) ending on lastDay, as the grid lays it out in the browser's zone.
const fixtureColumns = (lastDay) => {
  const { startUtc, cells } = weekWindow(lastDay);
  return windowColumns(startUtc, Intl.DateTimeFormat().resolvedOptions().timeZone, cells, "round");
};

// On team-draft-new (Fern, no usual week): goes to the next week (so none of it has passed), opens My availability,
// picks This week, paints the last day's 12 PM, and either presses Save as my usual week or leaves it saved as
// painted, then goes back to Everyone. Resolves with the root, the api, and that cell's free count before and after.
// The fixture's weeks follow the real clock, so the view runs on it too.
async function paintAndSave(keepAsUsual) {
  const api = createFixtureApi("team-draft-new", diskFetch);
  const root = new Node();
  const view = await mountTeamDraft(root, api, { now: () => new Date() });
  try {
    root.find((n) => n.attrs["aria-label"] === "Later week").click();
    await tick();
    // Noon on the window's last whole date, its cell taken from the same layout the editor draws.
    const cols = fixtureColumns(addDays(deadlineOnOrAfter(new Date()), 7));
    const noon = cols.at(-2).cells.find((c) => c.label.endsWith(", 12:00 PM"));
    const when = noon.label;
    const before = freeAt(root, when);

    byText(root, "My availability").click();
    await tick();
    byText(root, "This week").click();
    await tick();
    const grid = mineBox(root).find((n) => n.attrs.role === "grid");
    const target = mineBox(root).find((n) => n.attrs["data-i"] === String(noon.key));
    for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
    if (keepAsUsual) {
      byText(root, "Save as my usual week").click();
      await tick();
    }
    byText(root, "Everyone").click();
    await tick();
    return { root, api, before, after: freeAt(root, when) };
  } finally {
    view.dispose();
  }
}

test("changing this week saves overrides alone and the heatmap counts them", async () => {
  const { root, api, before, after } = await paintAndSave(false);
  assert.equal(after, before + 1);
  assert.equal(cardTitle(root), "You're counted from this week's changes");
  assert.equal(await api.getUsual(), null);
});

test("Save as my usual week turns this week into the usual week and drops the changes", async () => {
  const { root, api, before, after } = await paintAndSave(true);
  assert.equal(after, before + 1);
  assert.notEqual(await api.getUsual(), null);
  assert.equal((await api.getThisWeek()).days.length, 0);
  assert.equal(cardTitle(root), "You're counted from your usual week");
});

test("the card warns a viewer with nothing entered and opens My availability on the usual week", async () => {
  const api = createFixtureApi("team-draft-new", diskFetch);
  const root = new Node();
  const view = await mountTeamDraft(root, api, { now: () => new Date() });
  try {
    assert.equal(cardTitle(root), "The grid can't count you yet");
    const heading = root.children[1];
    byText(root, "My availability").click();
    await tick();
    assert.equal(cardTitle(root), "Changing your usual week");
    assert.equal(byText(root, "Usual week").attrs["aria-pressed"], "true");
    assert.equal(byText(root, "Save"), undefined);
    // The usual week is drawn as the matchup's window, the same days as Everyone's heatmap.
    const names = [];
    const walk = (n) => { if (n.className === "dayname") names.push(n.textContent); (n.children || []).forEach(walk); };
    walk(mineBox(root));
    assert.deepEqual(names, fixtureColumns(deadlineOnOrAfter(new Date())).map((c) => c.label));
    // A painted cell is saved without a Save press, once the viewer goes back to Everyone.
    const grid = mineBox(root).find((n) => n.attrs.role === "grid");
    const target = mineBox(root).find((n) => n.attrs["data-i"] === "24");
    for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
    byText(root, "Everyone").click();
    await tick();
    assert.notEqual(await api.getUsual(), null);
    assert.equal(cardTitle(root), "You're counted from your usual week");
    // The page around the pane was never redrawn: the same heading node throughout.
    assert.equal(root.children[1], heading);
  } finally {
    view.dispose();
  }
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

test("Show times in redraws the matchup in the picked zone and remembers it", async () => {
  const kept = new Map();
  const storage = { getItem: (k) => kept.get(k) ?? null, setItem: (k, v) => kept.set(k, v) };
  const zones = ["America/New_York", "Asia/Tokyo"];
  const { root, view } = await mount(fakeApi([], () => doc), { zonePicker: { zones, storage } });
  try {
    const cellLabel = () => root.find((n) => n.attrs["data-j"] === "0").attrs["aria-label"];
    const before = cellLabel();
    const was = activeTab(root).textContent;
    pickZone(root, "Asia/Tokyo");
    assert.match(root.find((n) => n.className === "lede").textContent, /Times in Asia\/Tokyo\./);
    assert.notEqual(cellLabel(), before);
    assert.deepEqual([...kept.values()], ["Asia/Tokyo"]);
    assert.equal(activeTab(root).textContent, was); // same date as before the change
  } finally {
    view.dispose();
  }
});

const pickZone = (root, zone) => { const s = root.find((n) => n.attrs.id === "shown-zone"); s.value = zone; for (const fn of s.listeners.change) fn({}); };
const allOf = (n, test, out = []) => { if (test(n)) out.push(n); for (const c of n.children) if (c.find) allOf(c, test, out); return out; };
const activeTab = (root) => allOf(root, (n) => n.attrs.role === "tab")[Number(root.find((n) => n.attrs.role === "grid").dataset.active)];

test("the day tab a viewer picked stays picked across a zone change", async () => {
  const zones = ["America/New_York", "Asia/Tokyo"];
  const { root, view } = await mount(fakeApi([], () => doc), { zonePicker: { zones, storage: null } });
  try {
    allOf(root, (n) => n.attrs.role === "tab")[2].click();
    const was = activeTab(root).textContent;
    assert.notEqual(was, allOf(root, (n) => n.attrs.role === "tab")[0].textContent);
    pickZone(root, "Asia/Tokyo");
    assert.equal(activeTab(root).textContent, was); // a new grid, open on the same date
  } finally {
    view.dispose();
  }
});

test("gridFrame opens on the column dated openDate, else the first, and reports the open date", () => {
  const columns = ["2026-10-24", "2026-10-25", "2026-10-26"].map((date, i) => ({ date, label: `D${i}`, start: 0, cells: [] }));
  const frame = gridFrame(columns, { role: "grid" }, () => new Node(), "2026-10-26");
  assert.equal(frame.grid.dataset.active, "2");
  assert.equal(frame.openDate(), "2026-10-26");
  frame.setDay(1);
  assert.equal(frame.openDate(), "2026-10-25");
  assert.equal(gridFrame(columns, { role: "grid" }, () => new Node(), "2030-01-01").grid.dataset.active, "0");
});

// Round 3 of a tournament: Sunday to Sunday Pacific, 384 cells, booked anywhere inside it.
const r3 = { ...doc, window: { start_utc: "2026-11-15T08:00:00Z", cell_minutes: 30, cells: 384, shape: "round" },
  round: { number: 3, first_day: "2026-11-15", last_day: "2026-11-22", prev: { round: 2 }, next: { round: 4 } },
  bookable: { start_utc: "2026-11-15T08:00:00Z", end_utc: "2026-11-23T08:00:00Z" } };
const roundNav = (root) => root.find((n) => n.className === "roundnav");
const byLabel = (root, label) => root.find((n) => n.attrs["aria-label"] === label);
const shown = (n) => n && !n.hidden;
const cellOf = (root, j) => root.find((n) => n.attrs["data-j"] === String(j));

test("the heading names the round and its dates", async () => {
  const { root, view } = await mount(fakeApi([], () => r3));
  try {
    assert.ok(byText(root, "Round 3 · Sun Nov 15 – Sun Nov 22"));
    assert.equal(shown(roundNav(root)), true);
    // Right under the heading.
    assert.equal(root.children[2], roundNav(root));
    // The grid names the round, not a rolling seven days.
    assert.match(root.find((n) => n.attrs.role === "grid").attrs["aria-label"], /^Round 3 · Sun Nov 15 – Sun Nov 22/);
  } finally {
    view.dispose();
  }
});

test("‹ fetches the round before, and coming back from My availability stays on it", async () => {
  const calls = [];
  const { root, view } = await mount(fakeApi(calls, () => r3));
  try {
    byLabel(root, "Earlier round").click();
    await tick();
    assert.deepEqual(calls.at(-1), ["get", "/api/team-draft?round=2"]);
    byText(root, "My availability").click();
    await tick();
    byText(root, "Everyone").click();
    await tick();
    assert.deepEqual(calls.filter((c) => c[0] === "get").at(-1), ["get", "/api/team-draft?round=2"]);
  } finally {
    view.dispose();
  }
});

test("an arrow pressed in My availability mounts the editor again on the new window", async () => {
  const calls = [];
  const r2 = { ...r3, window: { ...r3.window, start_utc: "2026-11-08T08:00:00Z" },
    round: { ...r3.round, number: 2, first_day: "2026-11-08", last_day: "2026-11-15", prev: { round: 1 }, next: { round: 3 } },
    bookable: { start_utc: "2026-11-08T08:00:00Z", end_utc: "2026-11-16T08:00:00Z" } };
  const api = { ...fakeApi(calls, (path) => (path.endsWith("?round=2") ? r2 : r3)), getThisWeek: async (range) => { calls.push(["week", range]); return { format: "draftbot.scheduler.this_week", version: 1, zone: null, days: [] }; } };
  const { root, view } = await mount(api);
  try {
    byText(root, "My availability").click();
    await tick();
    byLabel(root, "Earlier round").click();
    await tick();
    const reads = calls.filter((c) => c[0] === "week").map((c) => c[1].from);
    assert.equal(reads.length, 2);
    assert.ok(reads[1] < reads[0]);
  } finally {
    view.dispose();
  }
});

test("a booking from a browsed round posts with that round", async () => {
  const calls = [];
  const { root, view } = await mount(fakeApi(calls, () => r3));
  try {
    byLabel(root, "Earlier round").click();
    await tick();
    cellOf(root, 100).click();
    byText(root, "Book this time").click();
    await tick();
    assert.equal(calls.find((c) => c[0] === "post")[1], "/api/team-draft/actions?round=2");
  } finally {
    view.dispose();
  }
});

test("the first round has no ‹ and a rolling window has no round row", async () => {
  const first = { ...r3, round: { ...r3.round, number: 1, prev: null } };
  let { root, view } = await mount(fakeApi([], () => first));
  try {
    assert.equal(shown(byLabel(root, "Earlier round")), false);
    assert.equal(shown(byLabel(root, "Later round")), true);
  } finally {
    view.dispose();
  }
  ({ root, view } = await mount(fakeApi([], () => doc)));
  try {
    assert.equal(shown(roundNav(root)), false);
  } finally {
    view.dispose();
  }
});

test("an unnumbered week names its arrows by week", async () => {
  const week = { ...r3, round: { ...r3.round, number: null, prev: { week: "2026-11-08" }, next: { week: "2026-11-22" } } };
  const calls = [];
  const { root, view } = await mount(fakeApi(calls, () => week));
  try {
    assert.ok(byText(root, "Sun Nov 15 – Sun Nov 22"));
    assert.equal(shown(byLabel(root, "Earlier week")), true);
    byLabel(root, "Later week").click();
    await tick();
    assert.deepEqual(calls.at(-1), ["get", "/api/team-draft?week=2026-11-22"]);
  } finally {
    view.dispose();
  }
});

test("past cells are greyed and cannot be picked", async () => {
  const { root, view } = await mount(fakeApi([], () => r3), { now: () => new Date("2026-11-17T00:00:00Z") });
  try {
    assert.match(cellOf(root, 0).className, /\bpast\b/);
    assert.doesNotMatch(cellOf(root, 383).className, /\bpast\b/);
    cellOf(root, 0).click();
    assert.equal(shown(byText(root, "Book this time")), false);
    // Its names still show.
    assert.match(readoutOf(root).textContent, /Rook's team/);
    const keys = allOf(root, (n) => n.className === "legend")[0].children.map((k) => k.children?.[1]?.textContent);
    assert.ok(keys.includes("already passed"));
    assert.ok(!keys.includes("can't book here"));
  } finally {
    view.dispose();
  }
});

test("cells outside the dates open for booking are marked and named in the legend", async () => {
  const narrow = { ...r3, bookable: { start_utc: "2026-11-15T08:00:00Z", end_utc: "2026-11-22T08:00:00Z" } };
  const { root, view } = await mount(fakeApi([], () => narrow));
  try {
    assert.match(cellOf(root, 340).className, /\bnobook\b/);
    assert.doesNotMatch(cellOf(root, 300).className, /\bnobook\b/);
    const keys = allOf(root, (n) => n.className === "legend")[0].children.map((k) => k.children?.[1]?.textContent);
    assert.ok(keys.includes("can't book here"));
    assert.ok(!keys.includes("next round"));
  } finally {
    view.dispose();
  }
});

test("a booking on the trailing Sunday reads as booked", async () => {
  const start = Date.parse(r3.window.start_utc);
  const booked = { ...r3, booking: { ...doc.booking, start_utc: new Date(start + 380 * 1800000).toISOString().replace(/\.\d{3}Z$/, "Z"), version: 1 } };
  const { root, view } = await mount(fakeApi([], () => booked));
  try {
    for (const j of [380, 381]) assert.match(cellOf(root, j).className, /\bbooked\b/);
    assert.doesNotMatch(cellOf(root, 382).className, /\bbooked\b/);
  } finally {
    view.dispose();
  }
});

test("the first draw opens on today's tab when today is in view", async () => {
  const api = { ...fakeApi([], () => r3), getUsual: async () => ({ zone: "America/Los_Angeles" }) };
  const { root, view } = await mount(api, { now: () => new Date("2026-11-18T20:00:00Z"), zonePicker: { storage: null } });
  try {
    assert.equal(activeTab(root).textContent, "Wed 18");
  } finally {
    view.dispose();
  }
});

test("stepping to a round that is over opens on its first day, not the sliver of today at its end", async () => {
  const r2 = { ...r3, window: { ...r3.window, start_utc: "2026-11-08T08:00:00Z" },
    round: { ...r3.round, number: 2, first_day: "2026-11-08", last_day: "2026-11-15", prev: { round: 1 }, next: { round: 3 } },
    bookable: { start_utc: "2026-11-08T08:00:00Z", end_utc: "2026-11-16T08:00:00Z" } };
  // Mon 16 Nov, 7 AM New York: today's first hours, already gone, sit at the end of Round 2 in that zone.
  const api = { ...fakeApi([], (path) => (path.endsWith("?round=2") ? r2 : r3)), getUsual: async () => ({ zone: "America/New_York" }) };
  const { root, view } = await mount(api, { now: () => new Date("2026-11-16T12:00:00Z"), zonePicker: { storage: null } });
  try {
    assert.equal(activeTab(root).textContent, "Mon 16");
    byLabel(root, "Earlier round").click();
    await tick();
    assert.equal(activeTab(root).textContent, "Sun 8");
  } finally {
    view.dispose();
  }
});
