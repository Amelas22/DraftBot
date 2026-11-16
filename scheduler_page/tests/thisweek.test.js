import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { mountThisWeek, pastCells, prefill, shownCells, toThisWeekFile, withPainted } from "../js/thisweek.js";
import { decodeCells, decodeDayCells, encodeCells, encodeDayCells } from "../js/week.js";

const NY = "America/New_York";
const cell = (day, hour, half = 0) => day * 48 + hour * 2 + half;

const MON2 = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"];
// The window that opens at 10 AM on Monday 2 Nov in New York runs to 9:30 AM on Monday 9, over eight dates.
const START = new Date("2026-11-02T15:00:00Z");
const WINDOW_DATES = [...MON2, "2026-11-09"];
// The window cell at day (0 = Mon 2 .. 7 = Mon 9), hour and half in New York.
const jAt = (day, hour, half = 0) => cell(day, hour, half) - 20;

test("prefill shows an override on its date and the usual week elsewhere", () => {
  const usual = { zone: NY, cells: new Set([cell(0, 5), cell(1, 10), cell(6, 20)]) };
  const overrides = [{ date: "2026-11-03", cells: new Set([7]) }];
  assert.deepEqual(prefill(MON2, usual, overrides), new Set([cell(0, 5), cell(1, 0) + 7, cell(6, 20)]));
});

test("prefill lines the usual week up with each column's weekday", () => {
  const dates = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"];
  const usual = { zone: NY, cells: new Set([cell(0, 5)]) }; // Monday 5 AM
  assert.deepEqual(prefill(dates, usual, []), new Set([cell(5, 5)]));
});

test("prefill with no usual week and no overrides is empty", () => {
  assert.equal(prefill(MON2, null, []).size, 0);
});

test("shownCells puts each date's hours at their window cells, and only those in the window", () => {
  // Mon 2 at 5 AM is before the window opens; Mon 9 at 5 AM is inside it, and Tue 3 at 7 PM too.
  const days = new Set([cell(0, 5), cell(7, 5), cell(1, 19)]);
  assert.deepEqual(shownCells(WINDOW_DATES, days, START, NY), new Set([jAt(7, 5), jAt(1, 19)]));
});

test("withPainted sets the window's cells and keeps the hours outside it", () => {
  const days = new Set([cell(0, 5), cell(1, 19), cell(7, 20)]); // Mon 2 5 AM and Mon 9 8 PM are outside the window
  const out = withPainted(WINDOW_DATES, days, new Set([jAt(2, 12)]), START, NY);
  assert.deepEqual(out, new Set([cell(0, 5), cell(7, 20), cell(2, 12)]));
});

test("toThisWeekFile carries every date and never asks the server to make a usual week", () => {
  const f = toThisWeekFile(NY, WINDOW_DATES, new Set([cell(1, 10), cell(7, 0)]));
  assert.equal(f.format, "draftbot.scheduler.this_week");
  assert.equal(f.version, 1);
  assert.equal(f.zone, NY);
  assert.equal(f.also_usual, undefined);
  assert.deepEqual(f.days.map((d) => d.date), WINDOW_DATES);
  assert.deepEqual(decodeDayCells(f.days[1].cells), new Set([20]));
  assert.deepEqual(decodeDayCells(f.days[7].cells), new Set([0]));
  assert.equal(decodeDayCells(f.days[0].cells).size, 0);
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
  click() { for (const fn of this.listeners.click || []) fn({}); }
}

let saved;
before(() => {
  saved = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = { createElement: () => new Node() };
});
after(() => {
  if (saved) Object.defineProperty(globalThis, "document", saved);
  else delete globalThis.document;
});

const NOW = () => new Date("2026-11-02T15:00:00Z"); // Monday 2 Nov, 10 AM in New York
const USUAL = { format: "draftbot.scheduler.usual_week", version: 1, zone: NY, cells: encodeCells(new Set([cell(0, 5), cell(1, 10)])) };
const fakeApi = (calls, days = []) => ({
  getThisWeek: async () => ({ format: "draftbot.scheduler.this_week", version: 1, zone: days.length ? NY : null, days }),
  putThisWeek: async (doc) => { calls.push(["put", doc]); return doc; },
  clearThisWeek: async (range) => { calls.push(range ? ["clear", range] : ["clear"]); return null; },
});
// The root, with the editor's { flush } as root.editor. Unless deps say otherwise, it is just before the window opens.
const mount = async (api, deps) => {
  const root = new Node();
  root.editor = await mountThisWeek(root, api, { startUtc: START, now: () => new Date(START.getTime() - 1), ...deps });
  return root;
};
const WINDOW_RANGE = { from: "2026-11-02", to: "2026-11-09" };
// Toggles cell i with the keyboard, as a player pressing Space on it.
const press = (root, i) => {
  const grid = root.find((n) => n.attrs.role === "grid");
  const target = root.find((n) => n.attrs["data-i"] === String(i));
  for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
};
const byText = (root, text) => root.find((n) => n.textContent === text);
const tick = () => new Promise((r) => setImmediate(r));

test("with no usual week the editor offers a zone and Save as my usual week", async () => {
  const root = await mount(fakeApi([]), { usualDoc: null });
  assert.ok(byText(root, "Save as my usual week"));
  assert.ok(root.find((n) => n.attrs.id === "zone"));
});

test("Save as my usual week leaves no failed this-week save to put the changes back after it", async () => {
  const calls = [];
  // The first this-week save hangs until failInFlight; while the connection is down every other one fails too.
  let failInFlight;
  let down = true;
  const api = {
    ...fakeApi(calls),
    putThisWeek: (doc) => {
      calls.push(["put", doc]);
      if (calls.length === 1) return new Promise((_, no) => { failInFlight = no; });
      return down ? Promise.reject(new TypeError("fetch failed")) : Promise.resolve(doc);
    },
    putUsual: async (doc) => { calls.push(["usual", doc]); down = false; return doc; },
  };
  const root = await mount(api, { usualDoc: null, defaultZone: NY, zones: [NY] });
  press(root, jAt(0, 20));
  root.editor.flush();
  await tick(); // the save is sent and is still in flight when the button is pressed
  byText(root, "Save as my usual week").click();
  failInFlight(new TypeError("fetch failed"));
  for (let i = 0; i < 5; i++) await tick();
  // A later flush (leaving the editor) or the retry timer must not save this week again after the clear.
  await root.editor.flush();
  assert.deepEqual(calls.map((c) => c[0]), ["put", "put", "usual", "clear"]);
});

test("with a usual week there is neither a zone selector nor Save as my usual week", async () => {
  const root = await mount(fakeApi([]), { usualDoc: USUAL });
  assert.equal(byText(root, "Save as my usual week"), undefined);
  assert.equal(root.find((n) => n.attrs.id === "zone"), undefined);
});

test("Save as my usual week saves the painted window by weekday and drops this week's changes", async () => {
  const calls = [];
  let kept = null;
  const api = { ...fakeApi(calls), putUsual: async (doc) => { calls.push(["usual", doc]); return doc; } };
  // Mon 2 evening and Mon 9 morning are both inside the window, and both are Monday.
  const root = await mount(api, { usualDoc: null, defaultZone: NY, zones: [NY], onKeptAsUsual: (doc) => { kept = doc; } });
  press(root, jAt(0, 20));
  press(root, jAt(7, 5));
  byText(root, "Save as my usual week").click();
  await tick();
  await tick();
  assert.deepEqual(calls.map((c) => c[0]), ["usual", "clear"]);
  assert.equal(kept.zone, NY);
  assert.deepEqual(decodeCells(kept.cells), new Set([cell(0, 20), cell(0, 5)]));
});

test("the columns are the window's dates, as the matchup's heatmap shows them", async () => {
  const root = await mount(fakeApi([]), { usualDoc: USUAL });
  const names = [];
  const walk = (n) => { if (n.className === "dayname") names.push(n.textContent); (n.children || []).forEach(walk); };
  walk(root);
  assert.deepEqual(names, ["Mon 2", "Tue 3", "Wed 4", "Thu 5", "Fri 6", "Sat 7", "Sun 8", "Mon 9"]);
  // Monday 2 opens at 10 AM: nothing to paint before the window.
  assert.equal(root.find((n) => n.attrs["data-i"] === String(-1)), undefined);
});

test("the grid opens on the usual week with saved overrides shown instead", async () => {
  const root = await mount(fakeApi([], [{ date: "2026-11-03", cells: encodeDayCells(new Set([7])) }]), { usualDoc: USUAL });
  const on = (i) => root.find((n) => n.attrs["data-i"] === String(i)).classes.has("on");
  // Monday 5 AM is past on Mon 2, so it shows on Mon 9.
  assert.ok(on(jAt(7, 5)));
  assert.ok(on(jAt(1, 3, 1)));
  assert.ok(!on(jAt(1, 10)));
});

test("a change saves every date in the usual week's zone and hands the file on", async () => {
  const calls = [];
  let handed = null;
  const root = await mount(fakeApi(calls), { usualDoc: USUAL, onSaved: (doc) => { handed = doc; } });
  press(root, jAt(3, 20));
  await root.editor.flush();
  assert.ok(byText(root, "Saved."));
  assert.equal(root.find((n) => n.textContent === "Save"), undefined);
  const [kind, doc] = calls[0];
  assert.equal(kind, "put");
  assert.equal(doc.zone, NY);
  assert.equal(doc.also_usual, undefined);
  assert.equal(doc.days.length, 8);
  assert.deepEqual(decodeDayCells(doc.days[3].cells), new Set([40]));
  // Mon 2's 5 AM is before the window and is kept from the usual week.
  assert.deepEqual(decodeDayCells(doc.days[0].cells), new Set([10]));
  assert.equal(handed, doc);
});

test("a change with no usual week saves this week alone, in the chosen zone", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls), { usualDoc: null, defaultZone: NY, zones: [NY] });
  press(root, jAt(0, 10));
  await root.editor.flush();
  assert.deepEqual(calls.map((c) => c[0]), ["put"]);
  assert.equal(calls[0][1].zone, NY);
});

test("Reset to usual week drops the overrides and repaints from the usual week", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls, [{ date: "2026-11-03", cells: encodeDayCells(new Set([7])) }]), { usualDoc: USUAL });
  byText(root, "Reset to usual week").click();
  await tick();
  assert.deepEqual(calls, [["clear", WINDOW_RANGE]]);
  const on = (i) => root.find((n) => n.attrs["data-i"] === String(i)).classes.has("on");
  assert.ok(on(jAt(1, 10)));
  assert.ok(!on(jAt(1, 3, 1)));
});

test("nothing is saved until something changes", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls), { usualDoc: USUAL });
  await root.editor.flush();
  assert.deepEqual(calls, []);
});

test("Reset drops a change still waiting to save", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls), { usualDoc: USUAL });
  press(root, jAt(3, 20));
  byText(root, "Reset to usual week").click();
  await tick();
  await root.editor.flush();
  assert.deepEqual(calls, [["clear", WINDOW_RANGE]]);
});

test("a failed Reset says to press Reset again", async () => {
  const api = { ...fakeApi([]), clearThisWeek: async () => { throw new TypeError("fetch failed"); } };
  const root = await mount(api, { usualDoc: USUAL });
  byText(root, "Reset to usual week").click();
  await tick();
  assert.ok(byText(root, "Could not reset. Check your connection and press Reset again."));
});

const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);
const CELL = 1800000;

test("pastCells is every cell that has started", () => {
  assert.deepEqual(pastCells(START, 384, new Date(START.getTime() + 10 * CELL + 300000)), new Set(range(0, 11)));
  assert.deepEqual(pastCells(START, 384, new Date(START.getTime() - 1)), new Set());
});

// Drags from window cell a to b, as a finger would.
function drag(root, a, b) {
  const grid = root.find((n) => n.attrs.role === "grid");
  document.elementFromPoint = (x) => root.find((n) => n.attrs["data-i"] === String(x));
  try {
    for (const fn of grid.listeners.pointerdown) fn({ button: 0, pointerType: "touch", clientX: a, pointerId: 1 });
    for (const fn of grid.listeners.pointermove) fn({ clientX: b });
    for (const fn of grid.listeners.pointerup) fn({});
  } finally {
    delete document.elementFromPoint;
  }
}

test("past cells are locked: marked, unpressable, and a stroke over them keeps their saved state", async () => {
  const calls = [];
  // Mon 2 has a saved override: free at 10 AM (window cell 0) only.
  const api = fakeApi(calls, [{ date: "2026-11-02", cells: encodeDayCells(new Set([20])) }]);
  const root = await mount(api, { usualDoc: USUAL, now: () => new Date(START.getTime() + 10 * CELL + 300000) });
  const node = (j) => root.find((n) => n.attrs["data-i"] === String(j));
  const past = (j) => node(j).className.split(" ").includes("past");
  assert.ok(past(10));
  assert.equal(node(10).attrs["aria-disabled"], "true");
  assert.ok(!past(11));
  press(root, 5);
  assert.ok(!node(5).classes.has("on"));
  await root.editor.flush();
  assert.deepEqual(calls, []); // pressing a past cell is not a change
  drag(root, 5, 20); // a stroke cannot start on a past cell
  assert.ok(!node(15).classes.has("on"));
  drag(root, 20, 0); // erase-or-paint is decided by cell 20 (off), so this paints, but only cells 11..20
  await root.editor.flush();
  const [kind, doc] = calls[0];
  assert.equal(kind, "put");
  assert.deepEqual(decodeDayCells(doc.days[0].cells), new Set([20, ...range(31, 41)]));
});

test("a save sends the dates that are not over yet, so a day back to usual drops its old change", async () => {
  const calls = [];
  // Tue 3 at noon in New York: Mon 2 is over.
  const root = await mount(fakeApi(calls), { usualDoc: USUAL, now: () => new Date("2026-11-03T17:00:00Z") });
  press(root, jAt(3, 20));
  await root.editor.flush();
  assert.deepEqual(calls[0][1].days.map((d) => d.date), WINDOW_DATES.slice(1));
});

const LA = "America/Los_Angeles";
const ROUND = { startUtc: new Date("2026-11-15T08:00:00Z"), cells: 384, shape: "round" };
const USUAL_LA = { ...USUAL, zone: LA };

test("the editor reads and resets only the viewed dates", async () => {
  const calls = [];
  const reads = [];
  const api = { ...fakeApi(calls), getThisWeek: async (r) => { reads.push(r); return { format: "draftbot.scheduler.this_week", version: 1, zone: null, days: [] }; } };
  const root = await mount(api, { ...ROUND, usualDoc: USUAL_LA, now: () => new Date("2026-11-14T00:00:00Z") });
  assert.deepEqual(reads, [{ from: "2026-11-15", to: "2026-11-22" }]);
  byText(root, "Reset to usual week").click();
  await tick();
  assert.deepEqual(calls, [["clear", { from: "2026-11-15", to: "2026-11-22" }]]);
});

test("Reset keeps what the server keeps: the changes on days already over", async () => {
  const calls = [];
  // Mon 2 (over) and Wed 4 both have a change; it is Tue 3 at noon.
  const api = fakeApi(calls, [
    { date: "2026-11-02", cells: encodeDayCells(new Set([30])) }, { date: "2026-11-04", cells: encodeDayCells(new Set([30])) },
  ]);
  const root = await mount(api, { usualDoc: USUAL, now: () => new Date("2026-11-03T17:00:00Z") });
  byText(root, "Reset to usual week").click();
  await tick();
  const on = (i) => root.find((n) => n.attrs["data-i"] === String(i)).classes.has("on");
  assert.ok(on(jAt(0, 15)));
  assert.ok(!on(jAt(2, 15)));
});

test("a window entirely in the past is shown, says so, and never saves", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls), { ...ROUND, usualDoc: null, defaultZone: LA, zones: [LA], now: () => new Date("2026-11-24T00:00:00Z") });
  assert.ok(byText(root, "These days are over, so they can't be changed."));
  assert.equal(byText(root, "Reset to usual week"), undefined);
  assert.equal(byText(root, "Save as my usual week"), undefined);
  assert.ok(root.find((n) => n.attrs["data-i"] === "383").className.split(" ").includes("past"));
  press(root, 100);
  const select = root.find((n) => n.attrs.id === "zone");
  if (select) for (const fn of select.listeners.change || []) await fn({});
  await root.editor.flush();
  assert.deepEqual(calls, []);
});

test("a zone change whose dates can't be read keeps the old zone and never saves a date it hasn't read", async () => {
  const calls = [];
  const reads = [];
  const api = {
    ...fakeApi(calls),
    getThisWeek: async (r) => {
      reads.push(r);
      if (reads.length > 1) throw new TypeError("fetch failed");
      return { format: "draftbot.scheduler.this_week", version: 1, zone: null, days: [] };
    },
  };
  const root = await mount(api, { ...ROUND, usualDoc: null, defaultZone: LA, zones: [LA, NY], now: () => new Date("2026-11-14T00:00:00Z") });
  press(root, 100);
  const select = root.find((n) => n.attrs.id === "zone");
  select.value = NY; // in New York the window reaches Mon 23, which was never read
  for (const fn of select.listeners.change) await fn({});
  assert.deepEqual(reads, [{ from: "2026-11-15", to: "2026-11-22" }, { from: "2026-11-15", to: "2026-11-23" }]);
  assert.equal(select.value, LA);
  assert.ok(byText(root, "Could not change the time zone. Check your connection and try again."));
  await root.editor.flush();
  assert.equal(calls.length, 1);
  const [kind, doc] = calls[0];
  assert.equal(kind, "put");
  assert.equal(doc.zone, LA);
  assert.deepEqual(doc.days.map((d) => d.date), range(15, 23).map((d) => `2026-11-${d}`));
});
