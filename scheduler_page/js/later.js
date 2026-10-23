// The later-game model: when a player can play each pairing after the draft, with or without their
// library deck reserved, and the stand-in for the bot's answer to either request in fixture mode. Pure (no DOM).
import { CELLS, CELL_MS, startMs, instantOf, cellOfIso, hasRun } from "./week.js";
import { REFUSALS, timeLabel, runsOf, baseRequest, windowClosed, fakeDraftOutcome } from "./match.js";

// Draft wording, overridden where a later game reads differently.
export const SITTING_REFUSALS = {
  ...REFUSALS,
  not_bookable: "DraftBot's count says you and that opponent aren't both free then.",
  no_library: "Some of your deck's cards are held then.",
  not_in_match: "DraftBot doesn't have that opponent as your pairing in this match.",
};

// Two player lists naming the same people (a pairing, in either order).
export const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));
export const playerOf = (shared, user) => shared.teams.flatMap((t) => t.players).find((p) => p.user === user);

const isOpponent = (shared, me, user) => user !== me.user &&
  (shared.pairings || []).some((p) => p.players.includes(me.user) && p.players.includes(user));

export function myOpponents(shared, me) {
  const sittings = (shared.booking && shared.booking.sittings) || [];
  return (shared.pairings || [])
    .filter((p) => p.players.includes(me.user))
    .map((p) => {
      const user = p.players.find((u) => u !== me.user);
      const booked = sittings.find((s) => sameSet(s.players, [me.user, user])) || null;
      const player = playerOf(shared, user);
      return { user, name: player ? player.name : user, booked };
    });
}

export function deckOk(me, start) {
  return me.needs_library && me.deck && me.deck.assigned ? me.deck.free_starts.has(start) : true;
}

// Earliest allowed cell: now, and the draft's end once the draft is booked.
export function earliest(shared, nowUtc) {
  const draft = shared.booking && shared.booking.draft;
  const afterDraft = draft ? cellOfIso(shared, draft.start_utc) + shared.rules.draft_cells : 0;
  return Math.max(afterDraft, Math.ceil((nowUtc.getTime() - startMs(shared)) / CELL_MS), 0);
}

function bothFree(shared, me, opponent, start) {
  const a = playerOf(shared, me.user);
  const b = playerOf(shared, opponent);
  if (!a || !b) return false;
  const n = shared.rules.sitting_cells;
  return start + n <= CELLS && hasRun(a.cells, start, n) && hasRun(b.cells, start, n);
}

function sittingCheck(shared, me, opponent, start, nowUtc) {
  if (!Number.isInteger(start) || start < 0 || start + shared.rules.sitting_cells > CELLS) return "bad_time";
  if (start < earliest(shared, nowUtc)) return "bad_time";
  if (!bothFree(shared, me, opponent, start)) return "not_bookable";
  return null;
}

export function sittingStarts(shared, me, opponent, nowUtc, { reserve = false } = {}, zone = shared.window.display_zone) {
  const ok = new Map();
  for (let j = earliest(shared, nowUtc); j < CELLS; j++) {
    if (sittingCheck(shared, me, opponent, j, nowUtc)) continue;
    const dk = deckOk(me, j);
    if (reserve && !dk) continue;
    ok.set(j, { start: j, deckOk: dk });
  }
  // One offer per run of consecutive starts: the earliest, preferring one the deck can cover.
  return runsOf(ok)
    .map((starts) => ok.get(starts.find((j) => ok.get(j).deckOk) ?? starts[0]))
    .sort((a, b) => a.start - b.start)
    .map((c) => ({ ...c, label: timeLabel(instantOf(shared, c.start), zone) }));
}

export function sittingRequest(shared, opponent, start, reserve, now, uuid) {
  return { ...baseRequest(shared, "book_sitting", start, now, uuid), opponent, reserve_deck: !!reserve };
}

function fakeSittingOutcome(shared, me, request, nowUtc) {
  const refuse = (reason) => ({ status: "refused", reason });
  if (windowClosed(shared, nowUtc)) return refuse("window_closed");
  if (!isOpponent(shared, me, request.opponent)) return refuse("not_in_match");
  const cell = cellOfIso(shared, request.start_utc);
  const bad = sittingCheck(shared, me, request.opponent, cell, nowUtc);
  if (bad) return refuse(bad);
  if (request.reserve_deck && !deckOk(me, cell)) return refuse("no_library");
  return { status: "booked", reason: null };
}

// What the bot would answer to either request (file 4), from the same rules. A draft ignores the page's filters.
export const fakeOutcome = (shared, me, request, nowUtc) =>
  (request.kind === "book_sitting" ? fakeSittingOutcome(shared, me, request, nowUtc) : fakeDraftOutcome(shared, request, nowUtc));
