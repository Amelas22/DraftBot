import test from "node:test";
import assert from "node:assert/strict";
import { expiredMessage, loadFailedMessage, LOAD_FAILED, EXPIRED, NOT_ENABLED } from "../js/messages.js";
import { NeedsUpdate, NotEnabled } from "../js/api.js";

test("an expired link has one neutral sentence", () => {
  assert.equal(EXPIRED, "This link has expired. Press Find a team or My usual week in Discord for a new one.");
  assert.equal(expiredMessage(), EXPIRED);
  assert.equal(expiredMessage("usual"), EXPIRED);
});

test("a failed load says the page needs updating only for NeedsUpdate", () => {
  assert.equal(loadFailedMessage(new NeedsUpdate()), "This page needs updating.");
  assert.equal(loadFailedMessage(new Error("boom")), LOAD_FAILED);
});

test("a switched-off server says so", () => {
  assert.equal(NOT_ENABLED, "Find a team is turned off in this server right now.");
  assert.equal(loadFailedMessage(new NotEnabled()), NOT_ENABLED);
});
