import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseShared, parseMe } from "../js/match.js";
import { myOpponents, sittingStarts, sittingRequest, fakeSittingOutcome, SITTING_REFUSALS } from "../js/later.js";

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

test("Ben's opponents are Fay, already booked", () => {
  const { shared, me } = load();
  const o = myOpponents(shared, me);
  assert.equal(o.length, 1);
  assert.equal(o[0].user, FAY);
  assert.equal(o[0].name, "Fay");
  assert.equal(o[0].booked.start_utc, "2026-10-23T00:00:00Z");
});

test("starts: reserve drops Wednesday evening, no-reserve offers it; none before the draft's end or in the past", () => {
  const { shared, me, now } = load();
  const loose = sittingStarts(shared, me, FAY, now, { reserve: false }, ZONE);
  const tight = sittingStarts(shared, me, FAY, now, { reserve: true }, ZONE);
  const strict = tight.filter((s) => s.deckOk);
  assert.equal(strict.length, tight.length);
  const wed = (list) => list.filter((s) => /^Wed 21 Oct, (6|7|8|9):\d\d PM$/.test(s.label));
  assert.ok(wed(loose).length > 0, JSON.stringify(loose));
  assert.ok(wed(loose).every((s) => s.deckOk === false));
  assert.equal(wed(tight).length, 0);
  const drafEnd = cellOf(shared, "2026-10-21T00:00:00Z") + 2;
  for (const s of loose) assert.ok(s.start >= drafEnd && new Date(iso(shared, s.start)) >= now);
  assert.deepEqual(loose.map((s) => s.start), [...loose.map((s) => s.start)].sort((a, b) => a - b));
});

test("a start before the draft's end is excluded", () => {
  const { shared, me, now } = load();
  const early = new Date("2026-10-20T00:00:00Z");
  const r = fakeSittingOutcome(shared, me, sittingRequest(shared, FAY, cellOf(shared, "2026-10-21T00:30:00Z"), false, now, "u"), early);
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
  const { shared, me: me0, now } = load();
  const loose = sittingStarts(shared, me0, FAY, now, { reserve: false }, ZONE);
  const bad = loose.find((s) => !s.deckOk);
  // Give Ben a deck that is clear at that start so a reserved booking can succeed.
  const me = { ...me0, deck: { ...me0.deck, free_starts: new Set([...me0.deck.free_starts, bad.start]) } };
  const tight = sittingStarts(shared, me, FAY, now, { reserve: true }, ZONE)[0];
  assert.equal(tight.start, bad.start);
  const run = (opp, start, reserve, n = now) => fakeSittingOutcome(shared, me, sittingRequest(shared, opp, start, reserve, n, "u"), n);
  assert.deepEqual(run(FAY, tight.start, true), { status: "booked", reason: null }); // moves existing sitting
  assert.deepEqual(fakeSittingOutcome(shared, me0, sittingRequest(shared, FAY, bad.start, false, now, "u"), now), { status: "booked", reason: null });
  assert.equal(fakeSittingOutcome(shared, me0, sittingRequest(shared, FAY, bad.start, true, now, "u"), now).reason, "no_library");
  assert.equal(run(EVE, tight.start, true).reason, "not_in_match");
  assert.equal(run(ALI, tight.start, true).reason, "not_in_match");
  assert.equal(run(FAY, tight.start + 1000, false).reason, "bad_time");
  assert.equal(run(FAY, 5, false).reason, "bad_time");
  assert.equal(run(FAY, tight.start, true, new Date("2026-10-27T00:00:00Z")).reason, "window_closed");
  const s2 = { ...shared, match: { ...shared.match, status: "closed" } };
  assert.equal(fakeSittingOutcome(s2, me, sittingRequest(s2, FAY, tight.start, true, now, "u"), now).reason, "window_closed");
  assert.equal(SITTING_REFUSALS.no_library, "Some of your deck's cards are held then.");
  assert.ok(SITTING_REFUSALS.stale);
});

test("not_bookable when the opponent is busy", () => {
  const { shared, me, now } = load();
  const busy = Array.from({ length: 336 }, (_, i) => i).find((c) =>
    new Date(iso(shared, c)) > now && !shared.teams.flatMap((t) => t.players).find((p) => p.user === FAY).cells.has(c));
  const r = fakeSittingOutcome(shared, me, sittingRequest(shared, FAY, busy, false, now, "u"), now);
  assert.equal(r.reason, "not_bookable");
});
