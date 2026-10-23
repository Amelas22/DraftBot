import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseShared, parseMe, nobodyFollows } from "../js/match.js";
import { sittingRequest } from "../js/later.js";
import { laterTabShown, defaultTab, usesLibrary, sittingButtons, bookedLine, applySitting, pointsToLater } from "../js/laterview.js";

const fx = (name) =>
  JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", name), "utf8"));
const load = (s) => ({ shared: parseShared(fx(`match-shared.${s}.json`)), me: parseMe(fx(`match-me.${s}.json`)) });
const ZONE = "America/New_York";
const BEN = "900000000000000002", FAY = "900000000000000006";
const cellOf = (shared, iso) => (new Date(iso) - new Date(shared.window.start_utc)) / 1800000;
const texts = (bs) => bs.map((b) => [b.text, b.reserve]);

test("later tab: shown once the draft is booked; default for a viewer who doesn't play right after", () => {
  const { shared, me } = load("later");
  assert.equal(laterTabShown(shared, me), true);
  assert.equal(defaultTab(shared, me), "later");
});

test("later tab: a viewer who plays right after still defaults to the league match", () => {
  const { shared, me } = load("later");
  assert.equal(defaultTab(shared, { ...me, plays_after_draft: true }), "league");
  assert.equal(defaultTab(shared, { ...me, plays_after_draft: null }), "league");
});

test("later tab: nobody follows shows it before the draft is booked, but the league match is the default", () => {
  const { shared, me } = load("nofollow");
  assert.equal(laterTabShown(shared, me), true);
  assert.equal(defaultTab(shared, me), "league");
  const booked = { ...shared, booking: { ...shared.booking, draft: { start_utc: "2026-10-21T00:00:00Z", by: BEN } } };
  assert.equal(defaultTab(booked, me), "later");
});

test("later tab: hidden while unbooked and someone follows, and for a viewer with no pairing", () => {
  const { shared, me } = load("good");
  assert.equal(laterTabShown(shared, me), false);
  assert.equal(defaultTab(shared, me), "league");
  const later = load("later");
  assert.equal(laterTabShown({ ...later.shared, pairings: [] }, later.me), false);
  assert.equal(defaultTab({ ...later.shared, pairings: [] }, later.me), "league");
});

test("usesLibrary: needs the library, deck assigned, library on", () => {
  const { shared, me } = load("later");
  assert.equal(usesLibrary(shared, me), true);
  assert.equal(usesLibrary({ ...shared, library: { ...shared.library, enabled: false } }, me), false);
  assert.equal(usesLibrary(shared, { ...me, needs_library: false }), false);
  assert.equal(usesLibrary(shared, { ...me, deck: { ...me.deck, assigned: false } }), false);
  assert.equal(usesLibrary(shared, { ...me, deck: null }), false);
});

test("sittingButtons: library player gets reserve only where the deck is free", () => {
  const { shared, me } = load("later");
  const thu = cellOf(shared, "2026-10-22T23:00:00Z"), wed = cellOf(shared, "2026-10-21T22:00:00Z");
  assert.deepEqual(texts(sittingButtons(me, thu, { library: true })), [["Book + reserve", true], ["Book without library", false]]);
  assert.deepEqual(texts(sittingButtons(me, wed, { library: true })), [["Book without library", false]]);
});

test("sittingButtons: others just Book; a booked pairing moves", () => {
  const { shared, me } = load("later");
  const thu = cellOf(shared, "2026-10-22T23:00:00Z");
  assert.deepEqual(texts(sittingButtons(me, thu, { library: false })), [["Book", false]]);
  assert.deepEqual(texts(sittingButtons(me, thu, { library: false, moving: true })), [["Move here", false]]);
  assert.deepEqual(texts(sittingButtons(me, thu, { library: true, moving: true })), [["Move + reserve", true], ["Move without library", false]]);
});

test("bookedLine: with and without the deck", () => {
  const s = { players: [BEN, FAY], start_utc: "2026-10-22T23:00:00Z", cells: 3, by: BEN, decks_reserved: [BEN] };
  assert.equal(bookedLine(s, BEN, ZONE), "Booked: Thu 22 Oct, 7:00 PM, your deck is reserved.");
  assert.equal(bookedLine({ ...s, decks_reserved: [] }, BEN, ZONE), "Booked: Thu 22 Oct, 7:00 PM, no deck reserved.");
  assert.equal(bookedLine({ ...s, decks_reserved: [FAY] }, BEN, ZONE), "Booked: Thu 22 Oct, 7:00 PM, no deck reserved.");
});

test("applySitting: adds the pair's sitting, then a second booking replaces it", () => {
  const { shared, me } = load("later");
  const now = new Date(shared.generated_at);
  const thu = cellOf(shared, "2026-10-22T23:00:00Z"), wed = cellOf(shared, "2026-10-21T22:00:00Z");
  const other = { players: ["900000000000000003", "900000000000000007"], start_utc: "2026-10-24T00:00:00Z", cells: 3, by: "x", decks_reserved: [] };
  const base = { ...shared, booking: { ...shared.booking, sittings: [other] } };
  const one = applySitting(base, me, sittingRequest(base, FAY, thu, true, now, "a"));
  assert.deepEqual(one.booking.sittings, [other,
    { players: [BEN, FAY], start_utc: "2026-10-22T23:00:00Z", cells: 3, by: BEN, decks_reserved: [BEN] }]);
  const two = applySitting(one, me, sittingRequest(one, FAY, wed, false, now, "b"));
  assert.deepEqual(two.booking.sittings, [other,
    { players: [BEN, FAY], start_utc: "2026-10-21T22:00:00Z", cells: 3, by: BEN, decks_reserved: [] }]);
  assert.equal(base.booking.sittings.length, 1, "the input is not changed");
});

test("pointsToLater: a booked draft that nobody follows points to Later games", () => {
  const nf = load("nofollow");
  assert.equal(nobodyFollows(nf.shared), true);
  assert.equal(pointsToLater(nf.shared, nf.me), false, "not before the draft is booked");
  const booked = { ...nf.shared, booking: { ...nf.shared.booking, draft: { start_utc: "2026-10-21T00:00:00Z", by: BEN } } };
  assert.equal(pointsToLater(booked, nf.me), true);
  assert.equal(pointsToLater({ ...booked, pairings: [] }, nf.me), false, "no pairing, no Later games");
  const good = load("good");
  assert.equal(nobodyFollows(good.shared), false);
  const goodBooked = { ...good.shared, booking: { ...good.shared.booking, draft: { start_utc: "2026-10-21T00:00:00Z", by: BEN } } };
  assert.equal(pointsToLater(goodBooked, good.me), false, "someone follows");
});

test("applySitting: a move keeps the opponent's own reservation", () => {
  const { shared, me } = load("later");
  const now = new Date(shared.generated_at);
  const wed = cellOf(shared, "2026-10-21T22:00:00Z"), thu = cellOf(shared, "2026-10-22T23:00:00Z");
  const base = { ...shared, booking: { ...shared.booking, sittings: [
    { players: [FAY, BEN], start_utc: "2026-10-23T00:00:00Z", cells: 3, by: FAY, decks_reserved: [FAY, BEN] }] } };
  const moved = applySitting(base, me, sittingRequest(base, FAY, wed, false, now, "a"));
  assert.deepEqual(moved.booking.sittings[0].decks_reserved, [FAY]);
  const reserved = applySitting(base, me, sittingRequest(base, FAY, thu, true, now, "b"));
  assert.deepEqual(reserved.booking.sittings[0].decks_reserved, [BEN, FAY]);
});
