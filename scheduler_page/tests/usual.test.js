import test from "node:test";
import assert from "node:assert/strict";
import { applyStroke, toFile, loadWeek } from "../js/usual.js";
import { decodeCells } from "../js/week.js";
import { NotFound } from "../js/api.js";

const cell = (day, hour, half = 0) => day * 48 + hour * 2 + half;

test("applyStroke paints a rectangle", () => {
  const out = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  assert.equal(out.size, 12); // Tue-Thu x 19:00-20:30 = 3 days x 4 half-hours
  assert.ok(out.has(cell(2, 19, 1)));
  assert.ok(!out.has(cell(0, 19)));
});

test("applyStroke works whichever corner it starts from", () => {
  const a = applyStroke(new Set(), cell(3, 20, 1), cell(1, 19), true);
  assert.equal(a.size, 12);
});

test("applyStroke erases and does not mutate its input", () => {
  const start = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  const snapshot = new Set(start);
  const out = applyStroke(start, cell(2, 19), cell(2, 20, 1), false);
  assert.equal(out.size, 8);
  assert.deepEqual(start, snapshot);
});

test("toFile shape", () => {
  const cells = new Set([0, 1, 100, 335]);
  const f = toFile("America/New_York", cells, new Date("2026-10-22T12:34:56.789Z"));
  assert.equal(f.format, "draftbot.scheduler.usual_week");
  assert.equal(f.version, 1);
  assert.equal(f.zone, "America/New_York");
  assert.deepEqual(decodeCells(f.cells), cells);
  assert.equal(f.saved_at, "2026-10-22T12:34:56Z");
  assert.ok(f.saved_at.endsWith("Z"));
});

test("loadWeek treats NotFound as an empty week", async () => {
  const w = await loadWeek("u", async () => { throw new NotFound("u"); });
  assert.equal(w.cells.size, 0);
  assert.equal(w.zone, null);
});

test("loadWeek reads a file and refuses an unknown format", async () => {
  const ok = await loadWeek("u", async () => ({
    format: "draftbot.scheduler.usual_week", version: 1, zone: "Europe/Paris", cells: toFile("Europe/Paris", new Set([5]), new Date()).cells,
  }));
  assert.deepEqual(ok.cells, new Set([5]));
  assert.equal(ok.zone, "Europe/Paris");
  await assert.rejects(loadWeek("u", async () => ({ format: "x", version: 1 })));
  await assert.rejects(loadWeek("u", async () => ({ format: "draftbot.scheduler.usual_week", version: 2, cells: "" })));
});
