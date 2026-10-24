import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  decodeCells, encodeCells, windowCells, usualCellOf, CELLS,
} from "../js/week.js";

const fx = (name) =>
  JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", name), "utf8"));

test("conversion fixture cases match", () => {
  for (const c of fx("conversion.json").cases) {
    const got = encodeCells(windowCells(c.zone, decodeCells(c.usual_cells), new Date(c.window_start_utc)));
    assert.equal(got, c.expected_window_cells, c.name);
  }
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
