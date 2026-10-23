import test from "node:test";
import assert from "node:assert/strict";
import { expiredMessage, brokenMessage } from "../js/messages.js";

test("an unknown view gets the neutral wording", () => {
  for (const view of [undefined, null, "nope", "constructor"]) {
    assert.equal(expiredMessage(view), "This link has expired. Press the button in Discord again for a new one.");
    assert.equal(brokenMessage(view), "This link is broken. Press the button in Discord again for a new one.");
  }
});

test("the usual view points at /settings", () => {
  assert.equal(expiredMessage("usual"), "This link has expired. Press My usual week in /settings for a new one.");
  assert.equal(brokenMessage("usual"), "This link is broken. Press My usual week in /settings for a new one.");
});

test("the lfg view points at a card in #find-a-team", () => {
  assert.equal(expiredMessage("lfg"), "This link has expired. Press How well do we fit? on a card in #find-a-team for a new one.");
  assert.equal(brokenMessage("lfg"), "This link is broken. Press How well do we fit? on a card in #find-a-team for a new one.");
});
