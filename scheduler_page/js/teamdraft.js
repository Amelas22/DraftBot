// Team draft, the decisions: who is free when on each side, the runs of time the same people stay free, whether a
// cell can be booked, and what the booking and a refusal say. No DOM; the view draws it.
import { CELLS, CELL_MS, addCells } from "./week.js";
import { memberFree } from "./teams.js";

export const FORMAT = "draftbot.scheduler.team_draft";

// Per side, per person (in the document's order): the window cells they are free in.
export const freeBySide = (state, startUtc) => state.sides.map((side) => side.people.map((p) => memberFree(p, startUtc)));

// How many are free in cell j on each side, from freeBySide's sets, first side first.
export const sideCounts = (freeSides, j) => freeSides.map((side) => side.reduce((n, free) => n + (free.has(j) ? 1 : 0), 0));

// A cell's counts as the page shows them, side by side: "3–2".
export const countText = (counts) => counts.join("–");

// The matchup as the viewer has it switched on: the same state with the people in `off` (user ids) left out of
// every side, so the heatmap, counts and runs cover only who is still shown.
export const withoutPeople = (state, off) => ({
  ...state,
  sides: state.sides.map((side) => ({ ...side, people: side.people.filter((p) => !off.has(p.user)) })),
});

// Runs: stretches of cells where exactly the same people are free, on both sides, with at least one of them. Per window
// cell, null when nobody is free, else { first, last }: whether the cell opens or closes its run. A run never crosses
// into a new day column (columnStarts: the cells that begin one), since the grid draws each day separately.
export function freeRuns(freeSides, columnStarts) {
  const people = freeSides.flat();
  const who = (j) => people.map((free) => (free.has(j) ? "1" : "0")).join("");
  const keys = Array.from({ length: CELLS }, (_, j) => who(j));
  const joined = (j) => j > 0 && !columnStarts.has(j) && keys[j - 1] === keys[j];
  return keys.map((k, j) => (k.includes("1") ? { first: !joined(j), last: j === CELLS - 1 || !joined(j + 1) } : null));
}

// Pressing a side: switch all of it off, or all of it back on when it is already all off. Returns a new set.
export function toggleSide(off, side) {
  const users = side.people.map((p) => p.user);
  const next = new Set(off);
  if (users.every((u) => off.has(u))) users.forEach((u) => next.delete(u));
  else users.forEach((u) => next.add(u));
  return next;
}

// Pressing a person: switch them off or back on. Returns a new set.
export function togglePerson(off, user) {
  const next = new Set(off);
  if (!next.delete(user)) next.add(user);
  return next;
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
