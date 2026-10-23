import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  parseShared, parseMe, copyNeeded, freeCount, draftCheck, bestTimes, closestMisses,
  draftRequest, fakeOutcome, REFUSALS,
} from "../js/match.js";
import { CELLS } from "../js/week.js";

const fx = (name) =>
  JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", name), "utf8"));
const load = (s) => {
  const shared = parseShared(fx(`match-shared.${s}.json`));
  return { shared, me: parseMe(fx(`match-me.${s}.json`)), now: new Date(shared.generated_at) };
};
const NONE = new Set();
const ZONE = "America/New_York";
const startOf = (shared, iso) => (new Date(iso) - new Date(shared.window.start_utc)) / 1800000;
const ID = { Ali: "900000000000000001" };

test("parse refuses unknown format and newer version", () => {
  const doc = fx("match-shared.good.json");
  assert.throws(() => parseShared({ ...doc, format: "x" }), /needs updating/);
  assert.throws(() => parseShared({ ...doc, version: 2 }), /needs updating/);
  const me = fx("match-me.good.json");
  assert.throws(() => parseMe({ ...me, version: 2 }), /needs updating/);
  assert.ok(parseShared(doc).teams[0].players[0].cells instanceof Set);
  assert.ok(parseMe(me).deck.free_starts instanceof Set);
});

test("good: copy needed, at least 3 best times", () => {
  const { shared, now } = load("good");
  const c = copyNeeded(shared);
  assert.equal(c.needed, true);
  assert.deepEqual(c.who.sort(), ["Ben", "Fay"]);
  const best = bestTimes(shared, NONE, now, ZONE);
  assert.ok(best.length >= 3, JSON.stringify(best));
  assert.match(best[0].label, /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2}:\d{2} (AM|PM)$/);
  for (const b of best) assert.equal(draftCheck(shared, b.start, NONE, now).ok, true);
  // Wednesday never offered: copies are out all evening
  assert.ok(!best.some((b) => b.label.startsWith("Wed")));
});

test("good: Wednesday 20:00 fails only on the library", () => {
  const { shared, now } = load("good");
  const wed = startOf(shared, "2026-10-22T00:00:00Z");
  const r = draftCheck(shared, wed, NONE, now);
  assert.equal(r.ok, false);
  assert.equal(r.reason, "no_library");
});

test("nolib: same hour is bookable; nofollow needs no copy", () => {
  const { shared, now } = load("nolib");
  const wed = startOf(shared, "2026-10-22T00:00:00Z");
  assert.equal(draftCheck(shared, wed, NONE, now).ok, true);
  assert.equal(shared.library.enabled, true);
  assert.equal(copyNeeded(shared).needed, false);
  const nf = load("nofollow").shared;
  assert.equal(copyNeeded(nf).needed, false);
  assert.equal(draftCheck(nf, wed, NONE, now).ok, true);
});

test("copy need is false when the library is off", () => {
  const { shared } = load("good");
  shared.library.enabled = false;
  assert.equal(copyNeeded(shared).needed, false);
});

test("poor: none bookable, closest misses name who is missing", () => {
  const { shared, now } = load("poor");
  assert.deepEqual(bestTimes(shared, NONE, now, ZONE), []);
  const miss = closestMisses(shared, NONE, now, ZONE);
  assert.ok(miss.length >= 1 && miss.length <= 3);
  assert.ok(miss[0].missing.length >= 1);
  assert.ok(miss[0].shortfall >= 1);
  assert.ok(miss.every((m, i) => i === 0 || m.shortfall >= miss[i - 1].shortfall));
  assert.ok(miss.every((m) => typeof m.label === "string"));
});

test("scenario counts of bookable evening starts", () => {
  const evening = (shared, now) => {
    let n = 0;
    for (let j = 0; j < CELLS; j++) {
      const h = new Date(new Date(shared.window.start_utc).getTime() + j * 1800000)
        .toLocaleString("en-US", { timeZone: ZONE, hour: "numeric", hourCycle: "h23" });
      if (Number(h) >= 19 && Number(h) <= 22 && draftCheck(shared, j, NONE, now).ok) n++;
    }
    return n;
  };
  const exp = { good: 17, poor: 0, nolib: 22, nofollow: 22, later: 17 };
  for (const [s, n] of Object.entries(exp)) {
    const { shared, now } = load(s);
    assert.equal(evening(shared, now), n, s);
  }
});

test("later: Tuesday evening is past", () => {
  const { shared, now } = load("later");
  const tue = startOf(shared, "2026-10-21T00:00:00Z");
  const r = draftCheck(shared, tue, NONE, now);
  assert.equal(r.ok, false);
  assert.equal(r.reason, "past");
  assert.equal(draftCheck(shared, -1, NONE, now).reason, "outside");
  assert.equal(draftCheck(shared, CELLS - 1, NONE, now).reason, "outside");
});

test("filtering a player out lowers a team's count", () => {
  const { shared } = load("good");
  let j = -1;
  for (let i = 0; i < CELLS; i++) if (shared.teams[0].players.find((p) => p.name === "Ali").cells.has(i) && freeCount(shared, i, NONE)[0] >= 1) { j = i; break; }
  const before = freeCount(shared, j, NONE);
  const after = freeCount(shared, j, new Set([ID.Ali]));
  assert.equal(after[0], before[0] - 1);
  assert.equal(after[1], before[1]);
});

test("subs are team_size minus the minimum free", () => {
  const { shared, now } = load("good");
  const best = bestTimes(shared, NONE, now, ZONE, 50);
  for (const b of best) {
    const [a0, b0] = [0, 1].map((t) => Math.min(...[0, 1].map((k) => freeCount(shared, b.start + k, NONE)[t])));
    assert.deepEqual(draftCheck(shared, b.start, NONE, now).subs, [Math.max(0, 3 - a0), Math.max(0, 3 - b0)]);
  }
  for (let i = 1; i < best.length; i++) {
    assert.ok(best[i].subs > best[i - 1].subs || (best[i].subs === best[i - 1].subs && best[i].start > best[i - 1].start));
  }
});

test("a copy check running off the window end is no_library", () => {
  const { shared, now } = load("good");
  const s = CELLS - 2 - 1; // draft cells fit, copy cells (start+2 .. start+9) do not
  for (const t of shared.teams) for (const p of t.players) { p.cells.add(s); p.cells.add(s + 1); }
  assert.equal(draftCheck(shared, s, NONE, now).reason, "no_library");
});

test("bestTimes offers one start per block of consecutive bookable starts", () => {
  const { shared, now } = load("nolib");
  const best = bestTimes(shared, NONE, now, ZONE, 50);
  for (let i = 0; i < best.length; i++) for (let k = i + 1; k < best.length; k++) {
    assert.ok(Math.abs(best[i].start - best[k].start) > 1 || false);
  }
  assert.ok(bestTimes(shared, NONE, now, ZONE, 2).length === 2);
});

test("draftRequest shape", () => {
  const { shared } = load("good");
  const r = draftRequest(shared, 4, new Date("2026-10-19T14:03:30.123Z"), "u-1");
  assert.deepEqual(r, {
    format: "draftbot.scheduler.request", version: 1, request_id: "u-1", match_id: "4-3-7",
    kind: "book_draft", start_utc: "2026-10-19T06:00:00Z", seen_seq: 17, made_at: "2026-10-19T14:03:30Z",
  });
});

test("fakeOutcome follows the bot, ignoring page filters", () => {
  const { shared, now } = load("good");
  const best = bestTimes(shared, NONE, now, ZONE)[0];
  const req = (s) => draftRequest(shared, s, now, "u");
  assert.deepEqual(fakeOutcome(shared, req(best.start), now), { status: "booked", reason: null });
  const wed = startOf(shared, "2026-10-22T00:00:00Z");
  assert.deepEqual(fakeOutcome(shared, req(wed), now), { status: "refused", reason: "no_library" });
  assert.equal(fakeOutcome(shared, req(CELLS), now).reason, "bad_time");
  assert.equal(fakeOutcome(shared, { ...req(5), start_utc: "2026-10-19T04:10:00Z" }, now).reason, "bad_time");
  assert.equal(fakeOutcome({ ...shared, booking: { ...shared.booking, draft: { start_utc: "x" } } }, req(best.start), now).reason, "already_booked");
  assert.equal(fakeOutcome({ ...shared, match: { ...shared.match, status: "closed" } }, req(best.start), now).reason, "window_closed");
  const later = load("later");
  later.shared.booking = { ...later.shared.booking, draft: null };
  assert.equal(fakeOutcome(later.shared, draftRequest(later.shared, startOf(later.shared, "2026-10-21T00:00:00Z"), later.now, "u"), later.now).reason, "bad_time");
});

test("fakeOutcome refuses an hour only a filter made bookable as not_bookable", () => {
  const { shared, now } = load("poor");
  const miss = closestMisses(shared, NONE, now, ZONE)[0];
  // Filter nobody in, but also try the hour with every player off: page-only filters are ignored by the bot.
  const everyone = new Set(shared.teams.flatMap((t) => t.players.map((p) => p.user)));
  assert.equal(draftCheck(shared, miss.start, everyone, now).reason, "not_bookable");
  const out = fakeOutcome(shared, draftRequest(shared, miss.start, now, "u"), now);
  assert.equal(out.status, "refused");
  assert.equal(out.reason, "not_bookable");
});

test("a filtered-only-bookable hour is bookable under no filter? the reverse: fakeOutcome ignores off", () => {
  const { shared, now } = load("good");
  const best = bestTimes(shared, NONE, now, ZONE)[0];
  const off = new Set(shared.teams[0].players.map((p) => p.user));
  assert.equal(draftCheck(shared, best.start, off, now).reason, "not_bookable");
  assert.equal(fakeOutcome(shared, draftRequest(shared, best.start, now, "u"), now).status, "booked");
});

test("REFUSALS has a sentence for each code", () => {
  for (const c of ["not_bookable", "no_library", "already_booked", "not_in_match", "window_closed", "bad_time", "stale"]) {
    assert.match(REFUSALS[c], /\.$/);
  }
});
