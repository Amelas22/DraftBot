import test from "node:test";
import assert from "node:assert/strict";
import { layoutWindow } from "../js/grid.js";
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
