import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  FORMAT, bookable, bookingLine, countText, freeBySide, freeRuns, refusalText, shiftSelection, sideCounts, toggleSide,
  togglePerson, withoutPeople,
} from "../js/teamdraft.js";
import { encodeCells, CELLS } from "../js/week.js";

const fx = (n) => JSON.parse(readFileSync(new URL(`../fixtures/${n}`, import.meta.url)));
const base = fx("team-draft.json");
const start = new Date(base.window.start_utc);
const person = (user, zone, cells) => ({ user, name: user, zone, cells: cells ? encodeCells(cells) : null });
const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);
// A usual week painted on every day at the given local half-hour cells.
const daily = (cells) => new Set(range(0, 7).flatMap((d) => cells.map((c) => d * 48 + c)));
const doc = (a, b, extra = {}) => ({ ...base, ...extra, sides: [{ label: "A", people: a }, { label: "B", people: b }] });

test("FORMAT", () => assert.equal(FORMAT, "draftbot.scheduler.team_draft"));

test("freeBySide gives one set per person, in order; no week is never free", () => {
  const free = freeBySide(base, start);
  assert.deepEqual(free.map((s) => s.length), base.sides.map((s) => s.people.length));
  const noWeek = base.sides.flatMap((s, i) => s.people.map((p, k) => [p, free[i][k]])).filter(([p]) => p.cells === null);
  assert.ok(noWeek.length > 0);
  for (const [, f] of noWeek) assert.equal(f.size, 0);
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

test("shiftSelection keeps the picked time when the window start moves", () => {
  const t0 = new Date("2026-10-26T18:00:00Z");
  const later = (min) => new Date(t0.getTime() + min * 60000);
  assert.equal(shiftSelection(10, t0, t0), 10);
  assert.equal(shiftSelection(10, t0, later(30)), 9);
  assert.equal(shiftSelection(10, t0, later(90)), 7);
  assert.equal(shiftSelection(1, t0, later(30)), 0);
  assert.equal(shiftSelection(0, t0, later(30)), null, "a pick that slid before the window is dropped");
  assert.equal(shiftSelection(2, t0, later(120)), null);
  assert.equal(shiftSelection(null, t0, later(30)), null);
  assert.equal(shiftSelection(CELLS - 1, later(30), t0), null, "a pick pushed past the window's end is dropped");
});

test("countText reads side by side, first side first", () => {
  assert.equal(countText([3, 2]), "3–2");
  assert.equal(countText([0, 0]), "0–0");
});

test("sideCounts counts who is free in a cell on each side", () => {
  const free = [[new Set([1, 2]), new Set([2])], [new Set([2]), new Set(), new Set([1])]];
  assert.deepEqual(sideCounts(free, 2), [2, 1]);
  assert.deepEqual(sideCounts(free, 1), [1, 1]);
  assert.deepEqual(sideCounts(free, 0), [0, 0]);
});

test("withoutPeople drops the switched-off people from every side and leaves the rest alone", () => {
  const evening = daily(range(40, 46));
  const s = doc([person("a", "UTC", evening), person("b", "UTC", null)], [person("c", "UTC", evening), person("d", "UTC", evening)]);
  const out = withoutPeople(s, new Set(["b", "d"]));
  assert.deepEqual(out.sides.map((side) => side.people.map((p) => p.user)), [["a"], ["c"]]);
  assert.deepEqual(s.sides.map((side) => side.people.length), [2, 2]);
  assert.equal(out.booking, s.booking);
});

test("toggleSide switches a whole side off, or back on once all of it is off", () => {
  const side = { label: "A", people: [person("a", "UTC", null), person("b", "UTC", null)] };
  assert.deepEqual([...toggleSide(new Set(["x"]), side)].sort(), ["a", "b", "x"]);
  assert.deepEqual([...toggleSide(new Set(["a"]), side)].sort(), ["a", "b"]);
  assert.deepEqual([...toggleSide(new Set(["a", "b", "x"]), side)], ["x"]);
});

test("togglePerson flips one person and returns a new set", () => {
  const off = new Set(["a"]);
  assert.deepEqual([...togglePerson(off, "b")].sort(), ["a", "b"]);
  assert.deepEqual([...togglePerson(off, "a")], []);
  assert.deepEqual([...off], ["a"]);
});

// Two sides of free-cell sets, from per-person [first, last] cell ranges.
const sidesOf = (...sides) => sides.map((people) => people.map(([a, b]) => new Set(range(a, b + 1))));
const runAt = (runs, j) => runs[j] && [runs[j].first, runs[j].last];

test("freeRuns: the same people free over several cells is one run", () => {
  const runs = freeRuns(sidesOf([[10, 12]], [[10, 12]]), new Set([0]));
  assert.deepEqual([runAt(runs, 10), runAt(runs, 11), runAt(runs, 12)], [[true, false], [false, false], [false, true]]);
  assert.equal(runs[9], null);
  assert.equal(runs[13], null);
  assert.equal(runs.length, CELLS);
});

test("freeRuns: a new person in, or someone gone, starts a new run even when the counts stay the same", () => {
  // Side A rotates every cell (one person leaves as another arrives); side B stays put.
  const runs = freeRuns(sidesOf([[10, 10], [11, 11], [12, 12]], [[10, 12]]), new Set([0]));
  for (const j of [10, 11, 12]) assert.deepEqual(runAt(runs, j), [true, true]);
});

test("freeRuns: a change on either side splits the run", () => {
  const runs = freeRuns(sidesOf([[10, 13]], [[10, 11], [12, 13]]), new Set([0]));
  assert.deepEqual([runAt(runs, 11), runAt(runs, 12)], [[false, true], [true, false]]);
});

test("freeRuns: a run stops at the end of a day column", () => {
  const runs = freeRuns(sidesOf([[10, 13]], [[10, 13]]), new Set([0, 12]));
  assert.deepEqual([runAt(runs, 11), runAt(runs, 12)], [[false, true], [true, false]]);
});

test("freeRuns: nobody free is never a run", () => {
  assert.ok(freeRuns(sidesOf([[1, 1]], []), new Set([0])).every((r, j) => (j === 1 ? r !== null : r === null)));
  assert.ok(freeRuns(sidesOf([], []), new Set([0])).every((r) => r === null));
});
