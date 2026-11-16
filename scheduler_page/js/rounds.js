// Round and week arithmetic on the league's Pacific clock; twin of
// services/round_weeks.py. A window runs from 00:00 PT on the Sunday a week
// before its last day to 24:00 PT on its last day (a Sunday).
import { CELL_MS, addCells, localDateOf, noonOf } from "./week.js";
import { LEAGUE_ZONE } from "./grid.js";

const DAY_MS = 86400000;

// "YYYY-MM-DD" plus n days.
export function addDays(date, n) {
  return new Date(noonOf(date).getTime() + n * DAY_MS).toISOString().slice(0, 10);
}

// The instant 00:00 Pacific on a date: 07:00Z in daylight time, else 08:00Z.
export function leagueMidnight(date) {
  const daylight = new Date(`${date}T07:00:00Z`);
  return localDateOf(daylight, LEAGUE_ZONE) === date ? daylight : new Date(`${date}T08:00:00Z`);
}

// The Monday (Pacific, "YYYY-MM-DD") of the league week containing instant t.
export function leagueMonday(t) {
  const day = localDateOf(t, LEAGUE_ZONE);
  return addDays(day, -((noonOf(day).getUTCDay() + 6) % 7));
}

// Ascending Mondays of every league week overlapping [startUtc, endUtc).
export function leagueWeeksBetween(startUtc, endUtc) {
  const last = leagueMonday(new Date(endUtc.getTime() - 1));
  const out = [];
  for (let m = leagueMonday(startUtc); m <= last; m = addDays(m, 7)) out.push(m);
  return out;
}

// Ascending, unique league Mondays of the half-hours startUtc + 30 min * j.
export function weeksOfCells(cells, startUtc) {
  const weeks = new Set();
  for (const j of cells) weeks.add(leagueMonday(addCells(startUtc, j)));
  return [...weeks].sort();
}

// The free cells that count as confirmed: all when the person trusts their
// usual week (anything but an explicit false), else those inside a confirmed
// league week (confirmedRounds holds league Mondays).
export function confirmedCells(free, trustsUsual, confirmedRounds, startUtc) {
  if (trustsUsual !== false) return free;
  const spans = confirmedRounds.map((m) => [leagueMidnight(m).getTime(), leagueMidnight(addDays(m, 7)).getTime()]);
  const out = new Set();
  for (const j of free) {
    const t = startUtc.getTime() + j * CELL_MS;
    if (spans.some(([lo, hi]) => t >= lo && t < hi)) out.add(j);
  }
  return out;
}

// The window ending (inclusive) on lastDay: its UTC start and cell count.
export function weekWindow(lastDay) {
  const start = leagueMidnight(addDays(lastDay, -7));
  const end = leagueMidnight(addDays(lastDay, 1));
  return { startUtc: start, cells: Math.round((end - start) / CELL_MS) };
}

// The first Sunday, on the Pacific calendar, that is today or later.
export function deadlineOnOrAfter(now) {
  const today = localDateOf(now, LEAGUE_ZONE);
  const weekday = (noonOf(today).getUTCDay() + 6) % 7; // Monday = 0
  return addDays(today, (6 - weekday) % 7);
}

const dayFormat = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" });
const dayName = (date) => dayFormat.format(noonOf(date)).replace(",", "");

// "Round 3 · Sun Nov 15 – Sun Nov 22", or just the dates when unnumbered.
export function roundLabel(round) {
  const span = `${dayName(round.first_day)} – ${dayName(round.last_day)}`;
  return round.number == null ? span : `Round ${round.number} · ${span}`;
}

// The query string that addresses a prev/next reference from the state.
export function roundQuery(ref) {
  if (!ref) return "";
  if (ref.round != null) return `?round=${ref.round}`;
  if (ref.week != null) return `?week=${ref.week}`;
  return "";
}
