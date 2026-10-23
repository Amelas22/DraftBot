// The later-game model: when a player can play each pairing after the draft, with or without their
// library deck reserved, and a stand-in for the bot's answer in fixture mode. Pure (no DOM).
import { CELLS, CELL_MINUTES } from "./week.js";
import { REFUSALS, timeLabel, runsOf } from "./match.js";

const CELL_MS = CELL_MINUTES * 60000;

// Draft wording, overridden where a later game reads differently.
export const SITTING_REFUSALS = {
  ...REFUSALS,
  not_bookable: "DraftBot's count says you and that opponent aren't both free then.",
  no_library: "Some of your deck's cards are held then.",
  not_in_match: "DraftBot doesn't have that opponent as your pairing in this match.",
};

const startMs = (shared) => new Date(shared.window.start_utc).getTime();
const instantOf = (shared, j) => new Date(startMs(shared) + j * CELL_MS);
const isoSeconds = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));
const playerOf = (shared, user) => shared.teams.flatMap((t) => t.players).find((p) => p.user === user);

const opponentOf = (shared, me, user) => {
  const pr = (shared.pairings || []).find((p) => p.players.includes(me.user) && p.players.includes(user) && user !== me.user);
  return pr ? user : null;
};

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
  let from = 0;
  const draft = shared.booking && shared.booking.draft;
  if (draft) {
    const end = (new Date(draft.start_utc).getTime() - startMs(shared)) / CELL_MS + shared.rules.draft_cells;
    from = end;
  }
  const nowCell = Math.ceil((nowUtc.getTime() - startMs(shared)) / CELL_MS);
  return Math.max(from, nowCell, 0);
}

function bothFree(shared, me, opponent, start) {
  const a = playerOf(shared, me.user);
  const b = playerOf(shared, opponent);
  if (!a || !b) return false;
  const n = shared.rules.sitting_cells;
  if (start + n > CELLS) return false;
  for (let j = start; j < start + n; j++) if (!a.cells.has(j) || !b.cells.has(j)) return false;
  return true;
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
  return {
    format: "draftbot.scheduler.request",
    version: 1,
    request_id: uuid,
    match_id: shared.match.id,
    kind: "book_sitting",
    opponent,
    start_utc: isoSeconds(instantOf(shared, start)),
    reserve_deck: !!reserve,
    seen_seq: shared.seq,
    made_at: isoSeconds(now),
  };
}

// What the bot would answer, from the same rules.
export function fakeSittingOutcome(shared, me, request, nowUtc) {
  const refuse = (reason) => ({ status: "refused", reason });
  if (shared.match.status === "closed" || nowUtc.getTime() >= startMs(shared) + CELLS * CELL_MS) return refuse("window_closed");
  if (!opponentOf(shared, me, request.opponent)) return refuse("not_in_match");
  const cell = (new Date(request.start_utc).getTime() - startMs(shared)) / CELL_MS;
  const bad = sittingCheck(shared, me, request.opponent, cell, nowUtc);
  if (bad) return refuse(bad);
  if (request.reserve_deck && !deckOk(me, cell)) return refuse("no_library");
  return { status: "booked", reason: null };
}
