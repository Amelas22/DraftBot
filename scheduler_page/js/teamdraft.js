// Team draft, the decisions: when both sides have enough people free to draft, whether a cell can be booked, and
// what the booking and a refusal say. No DOM; the view draws it.
import { CELLS, CELL_MS, addCells } from "./week.js";
import { memberFree } from "./teams.js";

export const FORMAT = "draftbot.scheduler.team_draft";

// Per side, per person (in the document's order): the window cells they are free in.
export const freeBySide = (state, startUtc) => state.sides.map((side) => side.people.map((p) => memberFree(p, startUtc)));

// Window cells j where, on both sides, at least min(min_per_side, side size) people are free in every one of
// j .. j + draft_cells - 1. A draft that would run past the window's end never qualifies.
export function goodStarts(state, startUtc) {
  const { min_per_side: minPerSide, draft_cells: draftCells } = state.rules;
  const sides = freeBySide(state, startUtc).map((people) => ({ people, need: Math.min(minPerSide, people.length) }));
  const good = new Set();
  for (let j = 0; j + draftCells <= CELLS; j++) {
    const enough = sides.every(({ people, need }) => {
      let n = 0;
      for (const free of people) {
        let all = true;
        for (let k = j; k < j + draftCells && all; k++) all = free.has(k);
        if (all) n++;
      }
      return n >= need;
    });
    if (enough) good.add(j);
  }
  return good;
}

// Cell j may be booked when it is in the window, not yet started, and the matchup is still open.
export function bookable(j, state, now, startUtc) {
  if (state.matchup.state !== "open") return false;
  if (!Number.isInteger(j) || j < 0 || j >= CELLS) return false;
  return addCells(startUtc, j).getTime() > now.getTime();
}

// The window cell index that names the same moment as `selected` once the window starts at `newStartUtc` instead of
// `oldStartUtc`; null when nothing was picked or the moment is no longer in the window.
export function shiftSelection(selected, oldStartUtc, newStartUtc) {
  if (selected === null) return null;
  const j = selected - (newStartUtc.getTime() - oldStartUtc.getTime()) / CELL_MS;
  return Number.isInteger(j) && j >= 0 && j < CELLS ? j : null;
}

// "Booked for Fri 30 Oct, 8:00 PM by Bo" in the viewer's zone, or "No time booked yet".
export function bookingLine(state, zone) {
  const { start_utc: startUtc, by_name: byName } = state.booking;
  if (!startUtc) return "No time booked yet";
  const parts = {};
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hour12: true,
  });
  for (const p of format.formatToParts(new Date(startUtc))) parts[p.type] = p.value;
  const when = `${parts.weekday} ${parts.day} ${parts.month}, ${parts.hour}:${parts.minute} ${parts.dayPeriod}`;
  return byName ? `Booked for ${when} by ${byName}` : `Booked for ${when}`;
}

const REFUSALS = {
  changed: "Someone changed the booking first. Here is the latest.",
  past: "That time has already passed.",
  out_of_window: "That time is outside the next 7 days.",
  started: "This draft has already started.",
  gone: "This matchup no longer exists.",
  bad_input: "Something about that request wasn't valid. Reload and try again.",
  not_booked: "There is no booking to clear.",
};
const SERVER_FAILED = "DraftBot couldn't do that just now. Try again in a minute.";
export const refusalText = (code) => (Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : SERVER_FAILED);
