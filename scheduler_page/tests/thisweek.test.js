import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { columnLabel, editorDates, mountThisWeek, prefill, toThisWeekFile } from "../js/thisweek.js";
import { decodeDayCells, encodeCells, encodeDayCells } from "../js/week.js";

const NY = "America/New_York";
const cell = (day, hour, half = 0) => day * 48 + hour * 2 + half;

test("editorDates starts on today in the zone, not in UTC", () => {
  // 04:30 UTC on the 3rd is 23:30 on the 2nd in New York.
  const dates = editorDates(new Date("2026-11-03T04:30:00Z"), NY);
  assert.deepEqual(dates, ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]);
});

test("editorDates crosses a month end", () => {
  const dates = editorDates(new Date("2026-11-28T15:00:00Z"), NY);
  assert.equal(dates[0], "2026-11-28");
  assert.equal(dates[6], "2026-12-04");
});

test("columnLabel names the weekday, day and month", () => {
  assert.equal(columnLabel("2026-11-04"), "Wed 4 Nov");
  assert.equal(columnLabel("2026-12-20"), "Sun 20 Dec");
});

test("prefill shows an override on its date and the usual week elsewhere", () => {
  const dates = editorDates(new Date("2026-11-02T15:00:00Z"), NY); // Mon 2 .. Sun 8 Nov
  const usual = { zone: NY, cells: new Set([cell(0, 5), cell(1, 10), cell(6, 20)]) };
  const overrides = [{ date: "2026-11-03", cells: new Set([7]) }];
  assert.deepEqual(prefill(dates, usual, overrides), new Set([cell(0, 5), cell(1, 0) + 7, cell(6, 20)]));
});

test("prefill lines the usual week up with each column's weekday", () => {
  const dates = editorDates(new Date("2026-11-04T15:00:00Z"), NY); // Wed 4 .. Tue 10 Nov
  const usual = { zone: NY, cells: new Set([cell(0, 5)]) }; // Monday 5 AM
  assert.deepEqual(prefill(dates, usual, []), new Set([cell(5, 5)]));
});

test("prefill with no usual week and no overrides is empty", () => {
  const dates = editorDates(new Date("2026-11-02T15:00:00Z"), NY);
  assert.equal(prefill(dates, null, []).size, 0);
});

test("toThisWeekFile carries all seven days and also_usual", () => {
  const dates = editorDates(new Date("2026-11-02T15:00:00Z"), NY);
  const f = toThisWeekFile(NY, dates, new Set([cell(1, 10), cell(6, 0)]), true);
  assert.equal(f.format, "draftbot.scheduler.this_week");
  assert.equal(f.version, 1);
  assert.equal(f.zone, NY);
  assert.equal(f.also_usual, true);
  assert.deepEqual(f.days.map((d) => d.date), dates);
  assert.deepEqual(decodeDayCells(f.days[1].cells), new Set([20]));
  assert.deepEqual(decodeDayCells(f.days[6].cells), new Set([0]));
  assert.equal(decodeDayCells(f.days[0].cells).size, 0);
  assert.equal(toThisWeekFile(NY, dates, new Set(), false).also_usual, false);
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
  clearThisWeek: async () => { calls.push(["clear"]); return null; },
});
const mount = async (api, deps) => {
  const root = new Node();
  await mountThisWeek(root, api, { now: NOW, onSaved() {}, onBack() {}, backLabel: "Back to the matchup", ...deps });
  return root;
};
const byText = (root, text) => root.find((n) => n.textContent === text);
const tick = () => new Promise((r) => setImmediate(r));

test("with no usual week the editor offers a zone and the also-usual box, ticked", async () => {
  const root = await mount(fakeApi([]), { usualDoc: null });
  const box = root.find((n) => n.attrs.type === "checkbox");
  assert.ok(box);
  assert.equal(box.checked, true);
  assert.ok(byText(root, "Also keep this as my usual week"));
  assert.ok(root.find((n) => n.attrs.id === "zone"));
});

test("with a usual week there is neither a zone selector nor the also-usual box", async () => {
  const root = await mount(fakeApi([]), { usualDoc: USUAL });
  assert.equal(root.find((n) => n.attrs.type === "checkbox"), undefined);
  assert.equal(root.find((n) => n.attrs.id === "zone"), undefined);
});

test("the columns are the next seven dates", async () => {
  const root = await mount(fakeApi([]), { usualDoc: USUAL });
  const names = [];
  const walk = (n) => { if (n.className === "dayname") names.push(n.textContent); (n.children || []).forEach(walk); };
  walk(root);
  assert.deepEqual(names, ["Mon 2 Nov", "Tue 3 Nov", "Wed 4 Nov", "Thu 5 Nov", "Fri 6 Nov", "Sat 7 Nov", "Sun 8 Nov"]);
});

test("the grid opens on the usual week with saved overrides shown instead", async () => {
  const root = await mount(fakeApi([], [{ date: "2026-11-03", cells: encodeDayCells(new Set([7])) }]), { usualDoc: USUAL });
  const on = (i) => root.find((n) => n.attrs["data-i"] === String(i)).classes.has("on");
  assert.ok(on(cell(0, 5)));
  assert.ok(on(cell(1, 0) + 7));
  assert.ok(!on(cell(1, 10)));
});

test("Save sends all seven days in the usual week's zone and hands the file on", async () => {
  const calls = [];
  let handed = null;
  const root = await mount(fakeApi(calls), { usualDoc: USUAL, onSaved: (doc) => { handed = doc; } });
  const grid = root.find((n) => n.attrs.role === "grid");
  const target = root.find((n) => n.attrs["data-i"] === String(cell(3, 20)));
  for (const fn of grid.listeners.keydown) fn({ target, key: " ", preventDefault() {} });
  byText(root, "Save").click();
  await tick();
  const [kind, doc] = calls[0];
  assert.equal(kind, "put");
  assert.equal(doc.zone, NY);
  assert.equal(doc.also_usual, false);
  assert.equal(doc.days.length, 7);
  assert.deepEqual(decodeDayCells(doc.days[3].cells), new Set([40]));
  assert.deepEqual(decodeDayCells(doc.days[0].cells), new Set([10]));
  assert.equal(handed, doc);
});

test("Save with no usual week sends also_usual as ticked", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls), { usualDoc: null, defaultZone: NY, zones: [NY] });
  byText(root, "Save").click();
  await tick();
  assert.equal(calls[0][1].also_usual, true);
  assert.equal(calls[0][1].zone, NY);
});

test("Reset to usual week drops the overrides and repaints from the usual week", async () => {
  const calls = [];
  const root = await mount(fakeApi(calls, [{ date: "2026-11-03", cells: encodeDayCells(new Set([7])) }]), { usualDoc: USUAL });
  byText(root, "Reset to usual week").click();
  await tick();
  assert.deepEqual(calls, [["clear"]]);
  const on = (i) => root.find((n) => n.attrs["data-i"] === String(i)).classes.has("on");
  assert.ok(on(cell(1, 10)));
  assert.ok(!on(cell(1, 0) + 7));
});

test("the back button leaves without saving", async () => {
  const calls = [];
  let back = 0;
  const root = await mount(fakeApi(calls), { usualDoc: USUAL, onBack: () => { back += 1; } });
  byText(root, "Back to the matchup").click();
  assert.equal(back, 1);
  assert.deepEqual(calls, []);
});

test("a failed Reset says to press Reset again", async () => {
  const api = { ...fakeApi([]), clearThisWeek: async () => { throw new TypeError("fetch failed"); } };
  const root = await mount(api, { usualDoc: USUAL });
  byText(root, "Reset to usual week").click();
  await tick();
  assert.ok(byText(root, "Could not reset. Check your connection and press Reset again."));
});
