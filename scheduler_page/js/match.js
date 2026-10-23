// The match model: who is free, which draft hours are bookable, whether a library copy is needed, and a
// stand-in for the bot's answer in fixture mode. Pure (no DOM). Cell j is the half hour at
// window.start_utc + 30 min * j.
import { decodeCells, CELLS, CELL_MS, mergeBlocks, startMs, instantOf, cellOfIso, isoSeconds, hasRun } from "./week.js";
import { formatTime } from "./grid.js";
import { checkDoc } from "./api.js";

// "Tue 20 Oct, 7:00 PM" in the given zone: the heatmap's own label, so every view reads alike.
export { formatTime as timeLabel };

export const REFUSALS = {
  not_bookable: "DraftBot's count says a team doesn't have two of its own players free then.",
  no_library: "No league library copy is free for the games after the draft at that time.",
  already_booked: "Someone booked this match's draft first.",
  not_in_match: "DraftBot doesn't have you, or that opponent, in this match.",
  window_closed: "This match's week has ended or the match is closed.",
  bad_time: "That time has passed or isn't a half-hour slot in this match's week.",
  stale: "That request was too old when DraftBot saw it. Try again.",
};

export function parseShared(raw) {
  checkDoc(raw, "draftbot.scheduler.match");
  const doc = structuredClone(raw);
  return {
    ...doc,
    teams: doc.teams.map((t) => ({
      ...t,
      players: t.players.map((p) => ({ ...p, cells: decodeCells(p.cells) })),
    })),
  };
}

export function parseMe(raw) {
  checkDoc(raw, "draftbot.scheduler.match_me");
  const doc = structuredClone(raw);
  const deck = doc.deck;
  if (deck && typeof deck.free_starts === "string") deck.free_starts = decodeCells(deck.free_starts);
  return { ...doc, deck };
}

// The draft is booked: file 5 carries the booking, or says the match is booked.
export const draftBooked = (shared) => Boolean(shared.booking && shared.booking.draft) || shared.match.status === "booked";

// Nobody on either team plays right after the draft.
export const nobodyFollows = (shared) => !shared.teams.some((t) => t.players.some((p) => p.plays_after_draft === true));

export function copyNeeded(shared) {
  if (!shared.library || !shared.library.enabled) return { needed: false, who: [] };
  const who = shared.teams.flatMap((t) =>
    t.players.filter((p) => p.plays_after_draft === true && p.needs_library === true).map((p) => p.name));
  return { needed: who.length > 0, who };
}

export function freeCount(shared, j, off) {
  return shared.teams.map((t) => t.players.filter((p) => !off.has(p.user) && p.cells.has(j)).length);
}

function copyOk(shared, start) {
  const { copy_offset_cells: off, copy_cells: n } = shared.rules;
  const free = shared.library.copies_free;
  for (let j = start + off; j < start + off + n; j++) {
    if (j >= CELLS || !(free[j] >= 1)) return false;
  }
  return true;
}

export function draftCheck(shared, start, off, nowUtc) {
  const { draft_cells: dc, min_own_players: minOwn, team_size: size, max_subs: maxSubs } = shared.rules;
  if (!Number.isInteger(start) || start < 0 || start + dc > CELLS) return { ok: false, reason: "outside", subs: [0, 0] };
  if (instantOf(shared, start) < nowUtc) return { ok: false, reason: "past", subs: [0, 0] };
  const mins = [Infinity, Infinity];
  for (let j = start; j < start + dc; j++) {
    freeCount(shared, j, off).forEach((n, t) => { mins[t] = Math.min(mins[t], n); });
  }
  const subs = mins.map((m) => Math.max(0, size - m));
  if (mins.some((m) => m < minOwn) || subs.some((s) => s > maxSubs)) return { ok: false, reason: "not_bookable", subs };
  if (copyNeeded(shared).needed && !copyOk(shared, start)) return { ok: false, reason: "no_library", subs };
  return { ok: true, reason: null, subs };
}

// Consecutive-cell runs of the starts in `byStart` (a Map start -> info).
const runsOf = (byStart) => mergeBlocks(new Set(byStart.keys())).map(([a, b]) => {
  const starts = [];
  for (let j = a; j < b; j++) starts.push(j);
  return starts;
});

export function bestTimes(shared, off, nowUtc, zone = shared.window.display_zone, limit = 5) {
  const ok = new Map();
  for (let j = 0; j < CELLS; j++) {
    const r = draftCheck(shared, j, off, nowUtc);
    if (r.ok) ok.set(j, { start: j, subs: Math.max(...r.subs), teamSubs: r.subs });
  }
  // One offer per run: the start needing the fewest subs, earliest on a tie.
  return runsOf(ok)
    .map((starts) => starts.map((j) => ok.get(j)).reduce((best, c) => (c.subs < best.subs ? c : best)))
    .sort((a, b) => a.subs - b.subs || a.start - b.start)
    .slice(0, limit)
    .map((c) => ({ ...c, label: formatTime(instantOf(shared, c.start), zone) }));
}

// Why no hour works: starts that fail only on the library come first, then the smallest player shortfalls.
export function closestMisses(shared, off, nowUtc, zone = shared.window.display_zone) {
  const { draft_cells: dc, min_own_players: minOwn } = shared.rules;
  const found = new Map();
  for (let start = 0; start + dc <= CELLS; start++) {
    if (instantOf(shared, start) < nowUtc) continue;
    let shortfall = 0;
    const missing = [];
    shared.teams.forEach((t, ti) => {
      let least = Infinity;
      for (let j = start; j < start + dc; j++) least = Math.min(least, freeCount(shared, j, off)[ti]);
      if (least < minOwn) {
        shortfall += minOwn - least;
        for (const p of t.players) {
          const free = !off.has(p.user) && hasRun(p.cells, start, dc);
          if (!free) missing.push(p.name);
        }
      }
    });
    const label = formatTime(instantOf(shared, start), zone);
    if (shortfall > 0) found.set(start, { start, shortfall, missing, reason: "not_bookable", who: [], label });
    else if (draftCheck(shared, start, off, nowUtc).reason === "no_library") {
      found.set(start, { start, shortfall: 0, missing: [], reason: "no_library", who: copyNeeded(shared).who, label });
    }
  }
  // One per run of consecutive starts with the same reason, within one local day.
  const out = [];
  for (const starts of runsOf(found)) {
    let run = [];
    const flush = () => {
      if (run.length) out.push(run.reduce((b, c) => (c.shortfall < b.shortfall ? c : b)));
      run = [];
    };
    for (const j of starts) {
      const c = found.get(j);
      if (run.length && (run[0].reason !== c.reason || run[0].label.split(',')[0] !== c.label.split(',')[0])) flush();
      run.push(c);
    }
    flush();
  }
  return out.sort((a, b) => a.shortfall - b.shortfall || a.start - b.start).slice(0, 3);
}

// File 4 for a request of this kind starting at window cell `start`; a kind adds its own fields.
export function baseRequest(shared, kind, start, now, uuid) {
  return {
    format: "draftbot.scheduler.request",
    version: 1,
    request_id: uuid,
    match_id: shared.match.id,
    kind,
    start_utc: isoSeconds(instantOf(shared, start)),
    seen_seq: shared.seq,
    made_at: isoSeconds(now),
  };
}

export const draftRequest = (shared, start, now, uuid) => baseRequest(shared, "book_draft", start, now, uuid);

// The match takes no more bookings: it is closed or its week is over.
export const windowClosed = (shared, nowUtc) =>
  shared.match.status === "closed" || nowUtc.getTime() >= startMs(shared) + CELLS * CELL_MS;

// What the bot would answer, from the same rules. It ignores the page's filters.
export function fakeOutcome(shared, request, nowUtc) {
  const refuse = (reason) => ({ status: "refused", reason });
  if (windowClosed(shared, nowUtc)) return refuse("window_closed");
  if (shared.booking && shared.booking.draft) return refuse("already_booked");
  const cell = cellOfIso(shared, request.start_utc);
  if (!Number.isInteger(cell) || cell < 0 || cell >= CELLS) return refuse("bad_time");
  const r = draftCheck(shared, cell, new Set(), nowUtc);
  if (r.ok) return { status: "booked", reason: null };
  return refuse(r.reason === "past" || r.reason === "outside" ? "bad_time" : r.reason);
}
