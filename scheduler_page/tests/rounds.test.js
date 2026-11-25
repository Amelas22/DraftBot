import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { isoSeconds, addCells } from "../js/week.js";
import { weekWindow, deadlineOnOrAfter, addDays, roundLabel, roundQuery, leagueMidnight, leagueMonday, leagueWeeksBetween, weeksOfCells, confirmedCells } from "../js/rounds.js";

const fx = JSON.parse(readFileSync(new URL("../fixtures/rounds.json", import.meta.url), "utf8"));
const cfx = JSON.parse(readFileSync(new URL("../fixtures/confirmed.json", import.meta.url), "utf8"));

test("weekWindow reproduces rounds.json", () => {
  for (const c of fx.weeks) {
    const w = weekWindow(c.last_day);
    assert.equal(isoSeconds(w.startUtc), c.start_utc, c.name);
    assert.equal(w.cells, c.cells, c.name);
  }
});

test("deadlineOnOrAfter reproduces rounds.json", () => {
  for (const c of fx.deadlines) {
    assert.equal(deadlineOnOrAfter(new Date(c.now_utc)), c.deadline, c.name);
  }
});

test("roundLabel names the round and its two Sundays", () => {
  assert.equal(roundLabel({ number: 3, first_day: "2026-11-15", last_day: "2026-11-22" }), "Round 3 · Sun Nov 15 – Sun Nov 22");
  assert.equal(roundLabel({ number: null, first_day: "2026-11-15", last_day: "2026-11-22" }), "Sun Nov 15 – Sun Nov 22");
});

test("roundQuery", () => {
  assert.equal(roundQuery(null), "");
  assert.equal(roundQuery(undefined), "");
  assert.equal(roundQuery({ round: 2 }), "?round=2");
  assert.equal(roundQuery({ week: "2026-11-08" }), "?week=2026-11-08");
});

test("addDays crosses a month", () => assert.equal(addDays("2026-11-29", 7), "2026-12-06"));

test("leagueMonday reproduces confirmed.json", () => {
  for (const c of cfx.mondays) assert.equal(leagueMonday(new Date(c.at_utc)), c.monday, c.name);
});

test("leagueWeeksBetween reproduces confirmed.json", () => {
  for (const c of cfx.between) {
    const s = new Date(c.start_utc);
    assert.deepEqual(leagueWeeksBetween(s, addCells(s, c.cells)), c.weeks, c.name);
  }
});

test("weeksOfCells reproduces confirmed.json", () => {
  for (const c of cfx.painted) assert.deepEqual(weeksOfCells(c.cells, new Date(c.start_utc)), c.weeks, c.name);
});

test("confirmedCells reproduces confirmed.json", () => {
  for (const c of cfx.cells) {
    const got = [...confirmedCells(new Set(c.free), c.trusts_usual, c.confirmed_rounds, new Date(c.start_utc))];
    assert.deepEqual(got.sort((a, b) => a - b), c.expected, c.name);
  }
});

test("a missing trusts_usual reads as on", () =>
  assert.deepEqual(confirmedCells(new Set([0]), undefined, [], new Date("2026-11-15T08:00:00Z")), new Set([0])));

test("leagueMidnight", () => assert.equal(leagueMidnight("2026-11-16").toISOString(), "2026-11-16T08:00:00.000Z"));
