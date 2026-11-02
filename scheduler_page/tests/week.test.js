import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  decodeCells, encodeCells, windowCells, usualCellOf, CELLS,
  decodeDayCells, encodeDayCells, effectiveWindowCells,
  weekdayOfDate, usualWithWindow,
} from "../js/week.js";

const fx = (name) =>
  JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", name), "utf8"));

test("conversion fixture cases match", () => {
  for (const c of fx("conversion.json").cases) {
    const got = encodeCells(windowCells(c.zone, decodeCells(c.usual_cells), new Date(c.window_start_utc)));
    assert.equal(got, c.expected_window_cells, c.name);
  }
});

test("override fixture cases match", () => {
  for (const c of fx("override.json").cases) {
    const usual = c.usual && { zone: c.usual.zone, cells: decodeCells(c.usual.cells) };
    const overrides = c.overrides.map((o) => ({ zone: o.zone, date: o.date, cells: decodeDayCells(o.cells) }));
    const got = encodeCells(effectiveWindowCells(usual, overrides, new Date(c.window_start_utc)));
    assert.equal(got, c.expected_window_cells, c.name);
  }
});

test("day cells round trip", () => {
  const cells = new Set([0, 7, 47]);
  assert.deepEqual(decodeDayCells(encodeDayCells(cells)), cells);
  assert.throws(() => decodeDayCells(btoa("\0\0\0\0\0")));
});

test("round trip", () => {
  for (const name of ["usual-week.json"]) {
    const found = [];
    const walk = (o) => {
      if (Array.isArray(o)) o.forEach(walk);
      else if (o && typeof o === "object") {
        for (const [k, v] of Object.entries(o)) {
          if (k === "cells" && typeof v === "string") found.push(v);
          else walk(v);
        }
      }
    };
    walk(fx(name));
    assert.ok(found.length > 0, `${name} has cells fields`);
    for (const x of found) assert.equal(encodeCells(decodeCells(x)), x);
  }
});

test("bit order is MSB first", () => {
  assert.equal(Buffer.from(encodeCells([0]), "base64")[0], 0x80);
  assert.equal(Buffer.from(encodeCells([9]), "base64")[1], 0x40);
});

test("bad length throws", () => {
  assert.throws(() => decodeCells(btoa("x")));
});

test("usualCellOf reads local weekday and half hour", () => {
  assert.equal(usualCellOf(new Date("2026-10-19T04:00:00Z"), "America/New_York"), 0);
  assert.equal(usualCellOf(new Date("2026-10-26T03:59:00Z"), "America/New_York"), CELLS - 1);
});

test("addCells and isoSeconds place a window cell in time", async () => {
  const { addCells, isoSeconds, CELL_MS } = await import("../js/week.js");
  assert.equal(CELL_MS, 30 * 60000);
  assert.equal(isoSeconds(addCells(new Date("2026-10-19T04:00:00Z"), 3)), "2026-10-19T05:30:00Z");
  assert.equal(isoSeconds(new Date("2026-10-19T05:30:00.123Z")), "2026-10-19T05:30:00Z");
});

test("weekdayOfDate counts from Monday", () => {
  assert.equal(weekdayOfDate("2026-11-02"), 0);
  assert.equal(weekdayOfDate("2026-11-08"), 6);
  assert.equal(weekdayOfDate("2026-03-08"), 6); // a DST change day
});

test("usualWithWindow writes window cells back to their usual-week cells and keeps the rest", () => {
  const NY = "America/New_York";
  const start = new Date("2026-11-02T15:00:00Z"); // Monday 10 AM in New York
  // Window cell 0 is Monday 10 AM (usual cell 20); cell 2 is Monday 11 AM (22), painted before and now cleared.
  const out = usualWithWindow(new Set([22, 100]), new Set([0]), NY, start);
  assert.ok(out.has(20));
  assert.ok(!out.has(22));
  // Painting the whole window and then nothing round-trips through windowCells.
  assert.deepEqual(windowCells(NY, usualWithWindow(new Set(), new Set([0, 5, 300]), NY, start), start), new Set([0, 5, 300]));
  assert.ok(!out.has(100));
});
