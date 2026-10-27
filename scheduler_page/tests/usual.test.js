import test from "node:test";
import assert from "node:assert/strict";
import { applyStroke, toFile, loadWeek, saveFailedMessage, actionButtons, editWeekLabel } from "../js/usual.js";
import { decodeCells } from "../js/week.js";
import { NeedsUpdate } from "../js/api.js";

const cell = (day, hour, half = 0) => day * 48 + hour * 2 + half;

test("applyStroke paints a rectangle", () => {
  const out = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  assert.equal(out.size, 12);
  assert.ok(out.has(cell(2, 19, 1)));
  assert.ok(!out.has(cell(0, 19)));
});

test("applyStroke works whichever corner it starts from", () => {
  assert.equal(applyStroke(new Set(), cell(3, 20, 1), cell(1, 19), true).size, 12);
});

test("applyStroke erases and does not mutate its input", () => {
  const start = applyStroke(new Set(), cell(1, 19), cell(3, 20, 1), true);
  const snapshot = new Set(start);
  assert.equal(applyStroke(start, cell(2, 19), cell(2, 20, 1), false).size, 8);
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
});

test("loadWeek treats a never-saved week (null) as empty", async () => {
  const w = await loadWeek({ getUsual: async () => null });
  assert.equal(w.cells.size, 0);
  assert.equal(w.zone, null);
});

test("loadWeek reads a doc and refuses an unknown format", async () => {
  const doc = toFile("Europe/Paris", new Set([5]), new Date());
  const ok = await loadWeek({ getUsual: async () => doc });
  assert.deepEqual(ok.cells, new Set([5]));
  assert.equal(ok.zone, "Europe/Paris");
  await assert.rejects(loadWeek({ getUsual: async () => ({ format: "x", version: 1 }) }), NeedsUpdate);
  await assert.rejects(loadWeek({ getUsual: async () => ({ ...doc, version: 2 }) }), NeedsUpdate);
});

test("save failures say what went wrong", () => {
  const err = (code) => Object.assign(new Error(code), { code });
  assert.equal(saveFailedMessage(err("too_large")), "That week is too big to save.");
  assert.equal(saveFailedMessage(err("rate_limited")), "Too many saves just now. Wait a minute and press Save again.");
  assert.equal(saveFailedMessage(err("not_enabled")), "Find a team is turned off in this server right now.");
  assert.equal(saveFailedMessage(err("bad_input")), "Could not save. Check your connection and press Save again.");
  assert.equal(saveFailedMessage(new TypeError("fetch failed")), "Could not save. Check your connection and press Save again.");
});

test("actionButtons offers Back to teams only when there is somewhere to go back to", () => {
  assert.deepEqual(actionButtons(undefined), ["Save", "Clear"]);
  assert.deepEqual(actionButtons(() => {}), ["Save", "Clear", "Back to teams"]);
});

test("actionButtons names the page the back button returns to", () => {
  assert.deepEqual(actionButtons(() => {}, "Back to the matchup"), ["Save", "Clear", "Back to the matchup"]);
  assert.deepEqual(actionButtons(undefined, "Back to the matchup"), ["Save", "Clear"]);
});

test("editWeekLabel offers to set a week nobody has painted yet, else to edit it", () => {
  assert.equal(editWeekLabel(null), "Set my usual week");
  assert.equal(editWeekLabel({ zone: "Europe/London", cells: "" }), "Edit my usual week");
});
