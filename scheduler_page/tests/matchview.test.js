import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseShared } from "../js/match.js";
import { libraryLine } from "../js/matchview.js";

const shared = (s) => parseShared(JSON.parse(readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", `match-shared.${s}.json`), "utf8")));
const libraryOff = (sh) => ({ ...sh, library: { ...sh.library, enabled: false } });
const BOOKED_NOBODY = "Nobody plays right after the draft: each of you books your games under Later games.";
const NOBODY = "Nobody plays right after the draft. Once it's booked, each of you books your games under Later games.";

test("libraryLine: a copy is needed, held from the end of the draft", () => {
  assert.equal(libraryLine(shared("good")),
    "Booking holds one league copy for 4 hours from the end of the draft, for Ben and Fay (they play right after and need the library).");
});

test("libraryLine: a gap between the draft's end and the hold is said as such", () => {
  const sh = shared("good");
  const later = { ...sh, rules: { ...sh.rules, copy_offset_cells: sh.rules.draft_cells + 1 } };
  assert.equal(libraryLine(later),
    "Booking holds one league copy 30 minutes after the draft ends, for 4 hours, for Ben and Fay (they play right after and need the library).");
});

test("libraryLine: library on, nobody following needs a copy", () => {
  assert.equal(libraryLine(shared("nolib")), "No library copy is needed: nobody playing right after the draft needs one.");
});

test("libraryLine: nobody follows, library on or off", () => {
  assert.equal(libraryLine(shared("nofollow")), NOBODY);
  assert.equal(libraryLine(libraryOff(shared("nofollow"))), NOBODY);
});

test("libraryLine: nobody follows and the draft is already booked", () => {
  const sh = shared("nofollow");
  const booked = { ...sh, booking: { ...sh.booking, draft: { start_utc: "2026-10-21T00:00:00Z", by: "900000000000000002" } } };
  assert.equal(libraryLine(booked), BOOKED_NOBODY);
  assert.equal(libraryLine(libraryOff(booked)), BOOKED_NOBODY);
  assert.equal(libraryLine({ ...sh, match: { ...sh.match, status: "booked" } }), BOOKED_NOBODY);
});

test("libraryLine: library off hides the copy sentences", () => {
  assert.equal(libraryLine(libraryOff(shared("good"))), null);
  assert.equal(libraryLine(libraryOff(shared("nolib"))), null);
});
