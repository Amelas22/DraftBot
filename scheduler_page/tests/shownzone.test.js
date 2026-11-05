import test from "node:test";
import assert from "node:assert/strict";
import { rememberZone, shownZone } from "../js/shownzone.js";

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
