import test from "node:test";
import assert from "node:assert/strict";
import { layoutWindow, formatCell, formatTime, hourLabel, rowClass, windowColumns } from "../js/grid.js";
import { CELLS } from "../js/week.js";

const start = new Date("2026-10-21T04:00:00Z");

test("layoutWindow across the London fall-back has a 25-hour Sunday", () => {
  const days = layoutWindow(start, "Europe/London");
  const sunday = days.find((d) => d.date === "2026-10-25");
  assert.equal(sunday.cells.length, 50);
  assert.equal(sunday.cells[0].minutes, 0);
  const all = days.flatMap((d) => d.cells.map((c) => c.j));
  assert.equal(all.length, CELLS);
  assert.deepEqual([...all].sort((a, b) => a - b), Array.from({ length: CELLS }, (_, j) => j));
});

test("layoutWindow places a partial first day by its local time", () => {
  const days = layoutWindow(start, "Europe/London");
  assert.equal(days[0].date, "2026-10-21");
  assert.equal(days[0].cells[0].minutes, 5 * 60); // 04:00Z is 05:00 BST
  assert.equal(days[0].cells[0].j, 0);
  assert.match(days[0].label, /Wed/);
});

test("layoutWindow in a fixed zone is whole 48-cell days", () => {
  const days = layoutWindow(new Date("2026-10-19T04:00:00Z"), "America/New_York");
  assert.equal(days.length, 7);
  assert.ok(days.every((d) => d.cells.length === 48));
});

test("layoutWindow spring-forward day has 46 cells", () => {
  const days = layoutWindow(new Date("2026-03-25T00:00:00Z"), "Europe/London");
  assert.equal(days.find((d) => d.date === "2026-03-29").cells.length, 46);
});

test("formatCell reads weekday, day, short month and 12-hour time", () => {
  // 04:00Z is midnight in New York; cell 38 is 7:00 PM that Wednesday.
  assert.equal(formatCell(start, "America/New_York", 38), "Wed 21 Oct, 7:00 PM");
  assert.equal(formatCell(start, "America/New_York", 1), "Wed 21 Oct, 12:30 AM");
  assert.equal(formatCell(start, "Europe/London", 0), "Wed 21 Oct, 5:00 AM");
  assert.equal(formatTime(new Date("2026-10-25T23:30:00Z"), "Europe/London"), "Sun 25 Oct, 11:30 PM");
});

test("hourLabel", () => {
  assert.equal(hourLabel(0), "12 AM");
  assert.equal(hourLabel(12), "12 PM");
  assert.equal(hourLabel(19), "7 PM");
});

test("windowColumns is the window by local date, keyed by window cell, padded to its first half-hour", () => {
  const start = new Date("2026-11-02T15:00:00Z"); // Monday 10 AM in New York
  const cols = windowColumns(start, "America/New_York");
  assert.deepEqual(cols.map((c) => c.label), ["Mon 2", "Tue 3", "Wed 4", "Thu 5", "Fri 6", "Sat 7", "Sun 8", "Mon 9"]);
  assert.equal(cols[0].start, 20);
  assert.equal(cols[7].start, 0);
  assert.deepEqual(cols.flatMap((c) => c.cells.map((x) => x.key)), Array.from({ length: CELLS }, (_, j) => j));
  assert.equal(cols[0].cells[0].label, "Mon 2 Nov, 10:00 AM");
  assert.equal(cols[0].date, "2026-11-02");
});

test("rowClass opens each hour, closes it with its half-hour, and shades every other hour", () => {
  assert.deepEqual([0, 1, 2, 3, 4].map(rowClass), ["hr", "half", "hr alt", "half alt", "hr"]);
});

// Each column's cells' kinds, run-length: [["wk0", 48], ...] reads "48 cells of this week".
const kindRuns = (col) => col.cells.reduce((runs, c) => {
  const last = runs.at(-1);
  if (last && last[0] === c.kind) last[1]++; else runs.push([c.kind, 1]);
  return runs;
}, []);

test("the league's Sunday is due until midnight Pacific, 3 AM Eastern on Monday", () => {
  const cols = windowColumns(new Date("2026-11-05T15:00:00Z"), "America/New_York"); // Thursday 10 AM
  assert.deepEqual(cols.map((c) => [c.label, c.kind]), [
    ["Thu 5", "wk0"], ["Fri 6", "wk0"], ["Sat 7", "wk0"], ["Sun 8", "due"],
    ["Mon 9", "wk1"], ["Tue 10", "wk1"], ["Wed 11", "wk1"], ["Thu 12", "wk1"],
  ]);
  const sunday = cols.find((c) => c.label === "Sun 8");
  const monday = cols.find((c) => c.label === "Mon 9");
  // Midnight to 3 AM Eastern on Sunday is still Saturday in Pacific; Monday's first three hours are still Sunday.
  assert.deepEqual(kindRuns(sunday), [["wk0", 6], ["due", 42]]);
  assert.deepEqual(kindRuns(monday), [["due", 6], ["wk1", 42]]);
  const cutoffs = cols.flatMap((c) => c.cells.filter((x) => x.cutoff).map((x) => x.label));
  assert.deepEqual(cutoffs, ["Mon 9 Nov, 2:30 AM"]);
});

test("in London the deadline falls on the matching hours: until 8 AM Monday is still the league's Sunday", () => {
  const cols = windowColumns(new Date("2026-11-05T15:00:00Z"), "Europe/London");
  const sunday = cols.find((c) => c.label === "Sun 8");
  const monday = cols.find((c) => c.label === "Mon 9");
  assert.deepEqual(kindRuns(sunday), [["wk0", 16], ["due", 32]]);
  assert.deepEqual(kindRuns(monday), [["due", 16], ["wk1", 32]]);
  assert.equal(sunday.kind, "due");
  assert.equal(monday.kind, "wk1");
  const cutoffs = cols.flatMap((c) => c.cells.filter((x) => x.cutoff).map((x) => x.label));
  assert.deepEqual(cutoffs, ["Mon 9 Nov, 7:30 AM"]);
});

test("a window opening on the league's Sunday shows that deadline and next week's too", () => {
  const cols = windowColumns(new Date("2026-11-08T15:00:00Z"), "America/New_York"); // Sunday 10 AM
  assert.deepEqual(cols.map((c) => c.kind), ["due", "wk1", "wk1", "wk1", "wk1", "wk1", "wk1", "due"]);
  assert.equal(cols.flatMap((c) => c.cells.filter((x) => x.cutoff)).length, 1);
});
