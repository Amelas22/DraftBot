import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  FORMAT, bookable, bookingLine, cellState, countText, freeBySide, freeRuns, refusalText, shiftSelection, sideCounts, toggleSide,
  togglePerson, whoIsFree, whoText, withoutPeople, cellCountText, sureBySide, statusWeeks, isUnconfirmed,
} from "../js/teamdraft.js";
import { encodeCells, CELLS } from "../js/week.js";

const fx = (n) => JSON.parse(readFileSync(new URL(`../fixtures/${n}`, import.meta.url)));
const base = fx("team-draft.json");
const start = new Date(base.window.start_utc);
const person = (user, zone, cells) => ({ user, name: user, zone, cells: cells ? encodeCells(cells) : null });
const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);
// A usual week painted on every day at the given local half-hour cells.
const daily = (cells) => new Set(range(0, 7).flatMap((d) => cells.map((c) => d * 48 + c)));
const at = (j) => new Date(start.getTime() + j * 1800000);
const iso = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
const doc = (a, b, extra = {}) => ({ ...base, ...extra, sides: [{ label: "A", people: a }, { label: "B", people: b }] });

test("FORMAT", () => assert.equal(FORMAT, "draftbot.scheduler.team_draft"));

test("freeBySide gives one set per person, in order; no week is never free", () => {
  const free = freeBySide(base, start);
  assert.deepEqual(free.map((s) => s.length), base.sides.map((s) => s.people.length));
  const noWeek = base.sides.flatMap((s, i) => s.people.map((p, k) => [p, free[i][k]])).filter(([p]) => p.cells === null);
  assert.ok(noWeek.length > 0);
  // A person with only a this-week override is free just for that day's cells.
  for (const [p, f] of noWeek) assert.equal(f.size, p.overrides.length ? 4 : 0);
});

test("bookable: future, inside the window, matchup open", () => {
  const now = at(10);
  const open = { ...base, bookable: { start_utc: base.window.start_utc, end_utc: iso(at(CELLS)) } };
  assert.equal(bookable(20, open, now, start), true);
  assert.equal(bookable(10, open, now, start), false);
  assert.equal(bookable(5, open, now, start), false);
  assert.equal(bookable(CELLS, open, now, start), false);
  assert.equal(bookable(CELLS - 1, open, now, start), true);
  assert.equal(bookable(-1, open, now, start), false);
  const started = { ...open, matchup: { ...open.matchup, state: "started" } };
  assert.equal(bookable(20, started, now, start), false);
});

const round = { ...base, window: { ...base.window, cells: 384, shape: "round" },
  bookable: { start_utc: iso(at(48)), end_utc: iso(at(96)) } };
test("bookable: only inside the subject's span, and never in the past", () => {
  assert.equal(bookable(47, round, at(0), start), false); assert.equal(bookable(48, round, at(0), start), true);
  assert.equal(bookable(95, round, at(0), start), true); assert.equal(bookable(96, round, at(0), start), false);
  assert.equal(bookable(60, round, at(60), start), false); assert.equal(bookable(61, round, at(60), start), true);
  assert.equal(bookable(383, { ...round, bookable: { start_utc: iso(at(0)), end_utc: iso(at(384)) } }, at(0), start), true);
});
test("cellState: past wins, then outside the span", () => {
  assert.equal(cellState(10, round, at(20), start), "past"); assert.equal(cellState(30, round, at(20), start), "nobook");
  assert.equal(cellState(50, round, at(20), start), "");
  const started = { ...round, matchup: { ...round.matchup, state: "started" } };
  assert.equal(cellState(30, started, at(20), start), "", "a closed matchup is not marked unbookable");
});
test("freeRuns covers the whole window", () => assert.equal(freeRuns([[new Set([383])]], new Set([0]), 384).length, 384));
test("freeBySide covers the window's length", () => {
  const long = doc([person("a", "UTC", daily(range(0, 48)))], [], { window: { ...base.window, cells: 384 } });
  assert.ok(freeBySide(long, start)[0][0].has(383));
});
test("shiftSelection keeps a late cell of a long window", () => assert.equal(shiftSelection(380, start, start, 384), 380));
test("out_of_window names the booking dates", () => assert.equal(refusalText("out_of_window"), "That time is outside the dates open for booking."));

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

// Everyone on `sides` (arrays of names) free only in the cells given for them; the rest are never free.
const whoState = (sides) => ({ sides: sides.map(([label, ...names]) => ({ label, people: names.map((n) => ({ user: n, name: n })) })) });
const sets = (...cells) => cells.map((c) => new Set(c));

test("whoIsFree and whoText name who is free and who is not", () => {
  const state = whoState([["Team A", "Ann", "Bo", "Cy"], ["Team B", "Dee", "Eli", "Fay", "Gus"]]);
  const free = [sets([1], [1], [1]), sets([1], [1], [], [])];
  const groups = whoIsFree(state, free, 1);
  assert.deepEqual(groups, [
    { label: "Team A", free: ["Ann", "Bo", "Cy"], notFree: [] },
    { label: "Team B", free: ["Dee", "Eli"], notFree: ["Fay", "Gus"] },
  ]);
  assert.equal(whoText(groups), "Team A — Ann, Bo, Cy · Team B — Dee, Eli (Fay, Gus not free)");
});

test("whoText says nobody for a side with no one free", () => {
  const state = whoState([["Team A", "Ann"], ["Team B", "Dee"]]);
  const groups = whoIsFree(state, [sets([2]), sets([])], 2);
  assert.equal(whoText(groups), "Team A — Ann · Team B — nobody (Dee not free)");
});

test("whoText has no parenthetical when everyone is free", () => {
  const state = whoState([["Team A", "Ann"], ["Team B", "Dee"]]);
  assert.equal(whoText(whoIsFree(state, [sets([0]), sets([0])], 0)), "Team A — Ann · Team B — Dee");
});

// Only Ann is ever free (cell 0).
const freeBySideStub = (state) => state.sides.map((s) => s.people.map((p) => new Set(p.name === "Ann" ? [0] : [])));

test("a person switched off appears in neither list", () => {
  const state = withoutPeople(whoState([["Team A", "Ann", "Bo"], ["Team B", "Dee"]]), new Set(["Bo"]));
  const groups = whoIsFree(state, freeBySideStub(state), 0);
  assert.equal(whoText(groups), "Team A — Ann · Team B — nobody (Dee not free)");
});

// Round 3 window (Sun Nov 15 00:00 PT, 384 cells); Bo and Cy do not trust their usual week.
const r3start = new Date("2026-11-15T08:00:00Z");
const both = encodeCells([0, 48]);
const ann = { user: "a", name: "Ann", zone: "UTC", cells: both, trusts_usual: true, confirmed_rounds: [] };
const bo = { user: "b", name: "Bo", zone: "UTC", cells: both, trusts_usual: false, confirmed_rounds: [] };
const cy = { user: "c", name: "Cy", zone: "UTC", cells: both, trusts_usual: false, confirmed_rounds: ["2026-11-16"] };
const r3state = { ...base, round: { first_day: "2026-11-15", last_day: "2026-11-22" },
  window: { start_utc: "2026-11-15T08:00:00Z", cells: 384, shape: "round" },
  sides: [{ label: "Team A", people: [ann, bo] }, { label: "Team B", people: [cy] }] };
const allFree = [[new Set([0, 48]), new Set([0, 48])], [new Set([0, 48])]];
const r3sure = [[allFree[0][0], new Set()], [new Set([48])]];

test("sureBySide keeps confirmed cells only for people who do not trust their usual", () => {
  assert.deepEqual(sureBySide(r3state, allFree, r3start).map((s) => s.map((x) => [...x])), [[[0, 48], []], [[48]]]);
});
test("cellCountText shows probables as a suffix", () => {
  assert.equal(cellCountText(allFree, r3sure, 48), "1+1–1");
  assert.equal(cellCountText(allFree, r3sure, 0), "1+1–0+1");
});
test("cellCountText with everyone sure is today's text", () => assert.equal(cellCountText(allFree, allFree, 48), "2–1"));
test("countText", () => { assert.equal(countText([2, 3]), "2–3"); assert.equal(countText([2, 3], [1, 0]), "2+1–3"); });
test("whoText marks probable names", () =>
  assert.equal(whoText(whoIsFree(r3state, allFree, 48, r3sure)), "Team A — Ann, Bo (probably) · Team B — Cy"));
const one = (cells) => new Set(cells);
test("freeRuns without sureSides is unchanged", () => {
  const runs = freeRuns([[one(range(40, 56))]], new Set([0]), 384);
  assert.equal(runs[48].first, false);
});
test("freeRuns splits where a person turns sure", () => {
  const runs = freeRuns([[one(range(40, 56))]], new Set([0]), 384, [[one(range(48, 56))]]);
  assert.ok(runs[47].last && runs[48].first);
});
test("statusWeeks: the viewed round, or every week a rolling window touches", () => {
  assert.deepEqual(statusWeeks(r3state), ["2026-11-16"]);
  assert.deepEqual(statusWeeks({ ...r3state, round: null, window: { start_utc: "2026-11-20T17:00:00Z", cells: 336, shape: "rolling" } }),
    ["2026-11-16", "2026-11-23"]);
});
test("statusWeeks: a two-week round confirms both of its league weeks", () => {
  const round = { ...r3state.round, number: 4, first_day: "2026-11-22", last_day: "2026-12-06" };
  assert.deepEqual(statusWeeks({ ...r3state, round }), ["2026-11-23", "2026-11-30"]);
});
test("isUnconfirmed", () => {
  assert.deepEqual([ann, bo, cy].map((p) => isUnconfirmed(p, ["2026-11-16"])), [false, true, false]);
  assert.equal(isUnconfirmed(cy, ["2026-11-16", "2026-11-23"]), true);
  assert.equal(isUnconfirmed({ user: "x" }, ["2026-11-16"]), false);
});
