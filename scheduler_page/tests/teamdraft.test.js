import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FORMAT, freeBySide, goodStarts, bookable, bookingLine, refusalText } from "../js/teamdraft.js";
import { decodeCells, encodeCells, usualCellOf, CELLS } from "../js/week.js";

const fx = (n) => JSON.parse(readFileSync(new URL(`../fixtures/${n}`, import.meta.url)));
const base = fx("team-draft.json");
const start = new Date(base.window.start_utc);
const person = (user, zone, cells) => ({ user, name: user, zone, cells: cells ? encodeCells(cells) : null });
const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);
// A usual week painted on every day at the given local half-hour cells.
const daily = (cells) => new Set(range(0, 7).flatMap((d) => cells.map((c) => d * 48 + c)));
const doc = (a, b, extra = {}) => ({ ...base, ...extra, sides: [{ label: "A", people: a }, { label: "B", people: b }] });

// Independent expected value: walk the window and test each person's local cell.
function expectedGood(state, need = (side) => Math.min(state.rules.min_per_side, side.people.length)) {
  const out = new Set();
  const free = (p, j) => p.cells !== null && decodeCells(p.cells).has(usualCellOf(new Date(start.getTime() + j * 1800000), p.zone));
  for (let j = 0; j + state.rules.draft_cells <= CELLS; j++) {
    const ok = state.sides.every((side) => {
      const n = side.people.filter((p) => range(j, j + state.rules.draft_cells).every((k) => free(p, k))).length;
      return n >= need(side);
    });
    if (ok) out.add(j);
  }
  return out;
}

test("FORMAT", () => assert.equal(FORMAT, "draftbot.scheduler.team_draft"));

test("goodStarts matches an independent count on the fixture", () => {
  const want = expectedGood(base);
  assert.ok(want.size > 0);
  assert.deepEqual(goodStarts(base, start), want);
});

test("freeBySide gives one set per person, in order; no week is never free", () => {
  const free = freeBySide(base, start);
  assert.deepEqual(free.map((s) => s.length), base.sides.map((s) => s.people.length));
  const noWeek = base.sides.flatMap((s, i) => s.people.map((p, k) => [p, free[i][k]])).filter(([p]) => p.cells === null);
  assert.ok(noWeek.length > 0);
  for (const [, f] of noWeek) assert.equal(f.size, 0);
});

test("a one-person side needs only that person", () => {
  const evening = daily(range(40, 46));
  const s = doc([person("a", "UTC", evening)], [person("b", "UTC", evening)]);
  assert.deepEqual(goodStarts(s, start), expectedGood(s));
  assert.ok(goodStarts(s, start).size > 0);
});

test("a side with a week-less person still needs min_per_side when it is big enough", () => {
  const evening = daily(range(40, 46));
  const s = doc([person("a", "UTC", evening), person("b", "UTC", null)], [person("c", "UTC", evening), person("d", "UTC", evening)]);
  assert.equal(goodStarts(s, start).size, 0);
});

test("a cell needs every draft cell free", () => {
  // Only local cells 40 and 41 are free: starts at 40 work, 41 does not (41+1 = 42 is busy).
  const s = doc([person("a", "UTC", daily([40, 41]))], [person("b", "UTC", daily([40, 41]))]);
  const good = goodStarts(s, start);
  for (const j of good) assert.equal(usualCellOf(new Date(start.getTime() + j * 1800000), "UTC") % 48, 40);
  assert.ok(good.size > 0);
  assert.deepEqual(good, expectedGood(s));
});

test("the last cell of the window cannot start a draft", () => {
  const all = daily(range(0, 48));
  const s = doc([person("a", "UTC", all)], [person("b", "UTC", all)]);
  const good = goodStarts(s, start);
  assert.ok(good.has(CELLS - 2));
  assert.ok(!good.has(CELLS - 1));
  assert.equal(good.size, CELLS - 1);
});

test("bookable: future, inside the window, matchup open", () => {
  const at = (j) => new Date(start.getTime() + j * 1800000);
  const now = at(10);
  assert.equal(bookable(20, base, now, start), true);
  assert.equal(bookable(10, base, now, start), false);
  assert.equal(bookable(5, base, now, start), false);
  assert.equal(bookable(CELLS, base, now, start), false);
  assert.equal(bookable(CELLS - 1, base, now, start), true);
  assert.equal(bookable(-1, base, now, start), false);
  const started = { ...base, matchup: { ...base.matchup, state: "started" } };
  assert.equal(bookable(20, started, now, start), false);
});

test("bookingLine", () => {
  assert.equal(bookingLine(base, "America/New_York"), "No time booked yet");
  const booked = { ...base, booking: { start_utc: "2026-10-31T00:00:00Z", by_name: "Bo", version: 2 } };
  assert.equal(bookingLine(booked, "America/New_York"), "Booked for Fri 30 Oct, 8:00 PM by Bo");
  assert.equal(bookingLine(booked, "Europe/London"), "Booked for Sat 31 Oct, 12:00 AM by Bo");
});

test("every refusal code has its own words", () => {
  const codes = ["changed", "past", "out_of_window", "started", "gone", "bad_input", "not_booked"];
  const texts = codes.map(refusalText);
  for (const t of texts) assert.ok(t.length > 0);
  assert.equal(new Set(texts).size, codes.length);
  assert.ok(refusalText("nonsense").length > 0);
  assert.ok(!texts.includes(refusalText("nonsense")));
});
