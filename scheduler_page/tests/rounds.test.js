import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { isoSeconds } from "../js/week.js";
import { weekWindow, deadlineOnOrAfter, addDays, roundLabel, roundQuery } from "../js/rounds.js";

const fx = JSON.parse(readFileSync(new URL("../fixtures/rounds.json", import.meta.url), "utf8"));

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
