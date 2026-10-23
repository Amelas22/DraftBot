import test from "node:test";
import assert from "node:assert/strict";
import { plural, pluralWord } from "../js/words.js";

test("plural counts in words", () => {
  assert.equal(plural(1, "hour"), "1 hour");
  assert.equal(plural(0, "hour"), "0 hours");
  assert.equal(plural(2.5, "hour"), "2.5 hours");
  assert.equal(pluralWord(1, "sub"), "sub");
  assert.equal(pluralWord(3, "sub"), "subs");
});
