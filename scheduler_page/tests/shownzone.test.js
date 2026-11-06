import test from "node:test";
import assert from "node:assert/strict";
import { rememberZone, shownZone, shownZonePicker } from "../js/shownzone.js";

// A just-enough DOM for the picker: elements keep children, attributes, listeners and a class list.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.classes = new Set(); this.disabled = false; }
  append(...kids) { this.children.push(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get classList() { const c = this.classes; return { add: (x) => c.add(x), toggle: (x, on) => (on ? c.add(x) : c.delete(x)) }; }
}
const withDocument = async (fn) => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = { createElement: () => new Node() };
  try { return await fn(); } finally { if (saved) Object.defineProperty(globalThis, "document", saved); else delete globalThis.document; }
};
const selectOf = (picker) => picker.row.children.find((n) => n?.attrs?.id === "shown-zone");
const pick = (picker, zone) => { const s = selectOf(picker); s.value = zone; for (const fn of s.listeners.change) fn({}); };

const ZONES = ["America/New_York", "Europe/London"];
const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
};

test("with nothing remembered the page shows the fallback zone", () => {
  assert.equal(shownZone("America/New_York", ZONES, memory()), "America/New_York");
});

test("a remembered pick wins over the fallback", () => {
  const storage = memory();
  rememberZone("Europe/London", storage);
  assert.equal(shownZone("America/New_York", ZONES, storage), "Europe/London");
});

test("a remembered zone this browser no longer knows falls back", () => {
  const storage = memory();
  rememberZone("Mars/Olympus_Mons", storage);
  assert.equal(shownZone("America/New_York", ZONES, storage), "America/New_York");
});

test("a browser that refuses storage still shows the fallback and keeps working", () => {
  const refusing = { getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("QuotaExceeded"); } };
  assert.equal(shownZone("Europe/London", ZONES, refusing), "Europe/London");
  assert.doesNotThrow(() => rememberZone("America/New_York", refusing));
  assert.equal(shownZone("Europe/London", ZONES, null), "Europe/London");
});

test("a viewer who never picked follows a usual week saved in another zone; a picker does not", () => withDocument(() => {
  const drawn = [];
  const picker = shownZonePicker("America/New_York", (z) => drawn.push(z), { zones: ZONES, storage: memory() });
  picker.follow("Europe/London");
  assert.deepEqual(drawn, ["Europe/London"]);
  assert.equal(selectOf(picker).value, "Europe/London");
  picker.follow("Europe/London"); // already shown: nothing to draw
  picker.follow("Mars/Olympus_Mons"); // not a zone this browser knows
  assert.deepEqual(drawn, ["Europe/London"]);

  const storage = memory();
  rememberZone("Europe/London", storage);
  const chosen = shownZonePicker("America/New_York", (z) => drawn.push(z), { zones: ZONES, storage });
  chosen.follow("America/New_York");
  assert.deepEqual(drawn, ["Europe/London"]);
}));

test("picking a zone makes it stick: a later follow leaves it alone", () => withDocument(() => {
  const drawn = [];
  const picker = shownZonePicker("America/New_York", (z) => drawn.push(z), { zones: ZONES, storage: memory() });
  pick(picker, "Europe/London");
  picker.follow("America/New_York");
  assert.deepEqual(drawn, ["Europe/London"]);
}));

test("idle disables the picker and dims its row; off puts it back", () => withDocument(() => {
  const picker = shownZonePicker("America/New_York", () => {}, { zones: ZONES, storage: memory() });
  picker.idle(true);
  assert.equal(selectOf(picker).disabled, true);
  assert.ok(picker.row.classes.has("idle"));
  picker.idle(false);
  assert.equal(selectOf(picker).disabled, false);
  assert.ok(!picker.row.classes.has("idle"));
}));
