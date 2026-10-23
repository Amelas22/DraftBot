// The match model: who is free, which draft hours are bookable, whether a library copy is needed, and a
// stand-in for the bot's answer in fixture mode. Pure (no DOM). Cell j is the half hour at
// window.start_utc + 30 min * j.
import { decodeCells, CELLS, CELL_MINUTES, mergeBlocks } from "./week.js";

const CELL_MS = CELL_MINUTES * 60000;
const NEEDS_UPDATE = "This page needs updating.";

export const REFUSALS = {
  not_bookable: "DraftBot's count says a team doesn't have two of its own players free then.",
  no_library: "No league library copy is free for the games after the draft at that time.",
  already_booked: "Someone booked this match's draft first.",
  not_in_match: "DraftBot doesn't have you, or that opponent, in this match.",
  window_closed: "This match's week has ended or the match is closed.",
  bad_time: "That time has passed or isn't a half-hour slot in this match's week.",
  stale: "That request was too old when DraftBot saw it. Try again.",
};

function checkHeader(doc, format) {
  if (!doc || doc.format !== format || !(doc.version <= 1)) throw new Error(NEEDS_UPDATE);
}

export function parseShared(doc) {
  checkHeader(doc, "draftbot.scheduler.match");
  return {
    ...doc,
    teams: doc.teams.map((t) => ({
      ...t,
      players: t.players.map((p) => ({ ...p, cells: decodeCells(p.cells) })),
    })),
  };
}

export function parseMe(doc) {
  checkHeader(doc, "draftbot.scheduler.match_me");
  const deck = doc.deck ? { ...doc.deck } : doc.deck;
  if (deck && typeof deck.free_starts === "string") deck.free_starts = decodeCells(deck.free_starts);
  return { ...doc, deck };
}

const startMs = (shared) => new Date(shared.window.start_utc).getTime();
const instantOf = (shared, j) => new Date(startMs(shared) + j * CELL_MS);

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

const labelFormats = new Map();
function labelOf(date, zone) {
  let f = labelFormats.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: zone, weekday: "short", hour: "numeric", minute: "2-digit", hour12: true });
    labelFormats.set(zone, f);
  }
  const p = {};
  for (const part of f.formatToParts(date)) p[part.type] = part.value;
  return `${p.weekday} ${p.hour}:${p.minute} ${p.dayPeriod.toUpperCase()}`;
}

export function bestTimes(shared, off, nowUtc, zone = shared.window.display_zone, limit = 5) {
  const ok = new Map();
  for (let j = 0; j < CELLS; j++) {
    const r = draftCheck(shared, j, off, nowUtc);
    if (r.ok) ok.set(j, r);
  }
  const firsts = mergeBlocks(new Set(ok.keys())).map(([s]) => s);
  return firsts
    .map((start) => ({ start, subs: Math.max(...ok.get(start).subs), label: labelOf(instantOf(shared, start), zone) }))
    .sort((a, b) => a.subs - b.subs || a.start - b.start)
    .slice(0, limit);
}

export function closestMisses(shared, off, nowUtc, zone = shared.window.display_zone) {
  const { draft_cells: dc, min_own_players: minOwn } = shared.rules;
  const out = [];
  for (let start = 0; start + dc <= CELLS; start++) {
    if (instantOf(shared, start) < nowUtc) continue;
    let shortfall = 0;
    const missing = [];
    shared.teams.forEach((t) => {
      let least = Infinity;
      for (let j = start; j < start + dc; j++) least = Math.min(least, freeCount(shared, j, off)[shared.teams.indexOf(t)]);
      if (least < minOwn) {
        shortfall += minOwn - least;
        for (const p of t.players) {
          const free = !off.has(p.user) && [...Array(dc).keys()].every((k) => p.cells.has(start + k));
          if (!free) missing.push(p.name);
        }
      }
    });
    if (shortfall > 0) out.push({ start, shortfall, missing, label: labelOf(instantOf(shared, start), zone) });
  }
  return out.sort((a, b) => a.shortfall - b.shortfall || a.start - b.start).slice(0, 3);
}

const isoSeconds = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");

export function draftRequest(shared, start, now, uuid) {
  return {
    format: "draftbot.scheduler.request",
    version: 1,
    request_id: uuid,
    match_id: shared.match.id,
    kind: "book_draft",
    start_utc: isoSeconds(instantOf(shared, start)),
    seen_seq: shared.seq,
    made_at: isoSeconds(now),
  };
}

// What the bot would answer, from the same rules. It ignores the page's filters.
export function fakeOutcome(shared, request, nowUtc) {
  const refuse = (reason) => ({ status: "refused", reason });
  if (shared.match.status === "closed") return refuse("window_closed");
  if (shared.booking && shared.booking.draft) return refuse("already_booked");
  const cell = (new Date(request.start_utc).getTime() - startMs(shared)) / CELL_MS;
  if (!Number.isInteger(cell) || cell < 0 || cell >= CELLS) return refuse("bad_time");
  const r = draftCheck(shared, cell, new Set(), nowUtc);
  if (r.ok) return { status: "booked", reason: null };
  return refuse(r.reason === "past" || r.reason === "outside" ? "bad_time" : r.reason);
}
