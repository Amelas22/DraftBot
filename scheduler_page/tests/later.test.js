import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseShared, parseMe } from "../js/match.js";
import { myOpponents, sittingStarts, sittingRequest, fakeOutcome, sameSet, SITTING_REFUSALS } from "../js/later.js";

const fx = (name) =>
  JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", name), "utf8"));
const load = () => {
  const shared = parseShared(fx("match-shared.later.json"));
  return { shared, me: parseMe(fx("match-me.later.json")), now: new Date(shared.generated_at) };
};
const ZONE = "America/New_York";
const BEN = "900000000000000002", FAY = "900000000000000006", ALI = "900000000000000001", EVE = "900000000000000005";
const cellOf = (shared, iso) => (new Date(iso) - new Date(shared.window.start_utc)) / 1800000;
const iso = (shared, c) => new Date(new Date(shared.window.start_utc).getTime() + c * 1800000).toISOString();

test("Ben's opponents are Fay, no game booked yet", () => {
  const { shared, me } = load();
  const o = myOpponents(shared, me);
  assert.deepEqual(o.map((x) => [x.user, x.name, x.booked]), [[FAY, "Fay", null]]);
});

test("moving an existing sitting is booked", () => {
  const { shared, me, now } = load();
  const s = { ...shared, booking: { ...shared.booking, sittings: [
    { players: [FAY, BEN], start_utc: "2026-10-23T00:00:00Z", cells: 3, by: BEN, decks_reserved: [BEN] }] } };
  assert.equal(myOpponents(s, me)[0].booked.start_utc, "2026-10-23T00:00:00Z");
  const start = cellOf(s, "2026-10-22T23:00:00Z");
  assert.deepEqual(fakeOutcome(s, me, sittingRequest(s, FAY, start, true, now, "u"), now), { status: "booked", reason: null });
});

test("starts: reserve drops Wednesday, no-reserve offers it; Thursday is offered either way", () => {
  const { shared, me, now } = load();
  const loose = sittingStarts(shared, me, FAY, now, { reserve: false }, ZONE);
  const tight = sittingStarts(shared, me, FAY, now, { reserve: true }, ZONE);
  assert.deepEqual(loose.map((s) => [s.label, s.deckOk]), [["Wed 21 Oct, 6:00 PM", false], ["Thu 22 Oct, 7:00 PM", true]]);
  assert.deepEqual(tight.map((s) => [s.label, s.deckOk]), [["Thu 22 Oct, 7:00 PM", true]]);
  const drafEnd = cellOf(shared, "2026-10-21T00:00:00Z") + 2;
  for (const s of loose) assert.ok(s.start >= drafEnd && new Date(iso(shared, s.start)) >= now);
});

test("a start before the draft's end is excluded", () => {
  const { shared, me, now } = load();
  const early = new Date("2026-10-20T00:00:00Z");
  const r = fakeOutcome(shared, me, sittingRequest(shared, FAY, cellOf(shared, "2026-10-21T00:30:00Z"), false, early, "u"), early);
  assert.deepEqual(r, { status: "refused", reason: "bad_time" });
  const ok = sittingStarts(shared, me, FAY, early, { reserve: false }, ZONE);
  assert.ok(ok.every((s) => s.start >= cellOf(shared, "2026-10-21T00:00:00Z") + 2));
});

test("request shape", () => {
  const { shared, now } = load();
  const r = sittingRequest(shared, FAY, 100, true, now, "abc");
  assert.deepEqual(r, {
    format: "draftbot.scheduler.request", version: 1, request_id: "abc", match_id: "4-3-7",
    kind: "book_sitting", opponent: FAY, start_utc: iso(shared, 100).replace(".000Z", "Z"),
    reserve_deck: true, seen_seq: 17, made_at: "2026-10-21T14:00:00Z",
  });
});

test("fake outcomes in order", () => {
  const { shared, me, now } = load();
  const wed = cellOf(shared, "2026-10-21T22:00:00Z"); // Wed 6:00 PM NY
  const thu = cellOf(shared, "2026-10-22T23:00:00Z"); // Thu 7:00 PM NY
  const run = (opp, start, reserve, n = now) => fakeOutcome(shared, me, sittingRequest(shared, opp, start, reserve, n, "u"), n);
  assert.deepEqual(run(FAY, thu, true), { status: "booked", reason: null });
  assert.deepEqual(run(FAY, wed, false), { status: "booked", reason: null });
  assert.equal(run(FAY, wed, true).reason, "no_library");
  assert.equal(run(EVE, thu, true).reason, "not_in_match");
  assert.equal(run(ALI, thu, true).reason, "not_in_match");
  assert.equal(run(FAY, thu + 1000, false).reason, "bad_time");
  assert.equal(run(FAY, 5, false).reason, "bad_time");
  assert.equal(run(FAY, thu, true, new Date("2026-10-27T00:00:00Z")).reason, "window_closed");
  const s2 = { ...shared, match: { ...shared.match, status: "closed" } };
  assert.equal(fakeOutcome(s2, me, sittingRequest(s2, FAY, thu, true, now, "u"), now).reason, "window_closed");
  assert.equal(SITTING_REFUSALS.no_library, "Some of your deck's cards are held then.");
  assert.ok(SITTING_REFUSALS.stale);
});

test("not_bookable when the opponent is busy", () => {
  const { shared, me, now } = load();
  const busy = Array.from({ length: 336 }, (_, i) => i).find((c) =>
    new Date(iso(shared, c)) > now && !shared.teams.flatMap((t) => t.players).find((p) => p.user === FAY).cells.has(c));
  const r = fakeOutcome(shared, me, sittingRequest(shared, FAY, busy, false, now, "u"), now);
  assert.equal(r.reason, "not_bookable");
});

test("sameSet matches a pairing in either order", () => {
  assert.equal(sameSet(["1", "2"], ["2", "1"]), true);
  assert.equal(sameSet(["1", "2"], ["1", "3"]), false);
  assert.equal(sameSet(["1"], ["1", "2"]), false);
});

test("fakeOutcome answers a draft request with the draft rules", async () => {
  const { fakeDraftOutcome, draftRequest } = await import("../js/match.js");
  const { shared, me, now } = load();
  const req = draftRequest(shared, 40, now, "u");
  assert.deepEqual(fakeOutcome(shared, me, req, now), fakeDraftOutcome(shared, req, now));
  assert.equal(fakeOutcome(shared, me, req, now).reason, "already_booked");
});

test("a sitting request is file 4's base shape plus opponent and reserve_deck", async () => {
  const { baseRequest } = await import("../js/match.js");
  const { shared, now } = load();
  assert.deepEqual(sittingRequest(shared, FAY, 40, 1, now, "u"),
    { ...baseRequest(shared, "book_sitting", 40, now, "u"), opponent: FAY, reserve_deck: true });
});
