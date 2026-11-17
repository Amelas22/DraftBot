// Round and week arithmetic on the league's Pacific clock; twin of
// services/round_weeks.py. A window runs from 00:00 PT on the Sunday a week
// before its last day to 24:00 PT on its last day (a Sunday).
import { CELL_MS, localDateOf, noonOf } from "./week.js";
import { LEAGUE_ZONE } from "./grid.js";

const DAY_MS = 86400000;

// "YYYY-MM-DD" plus n days.
export function addDays(date, n) {
  return new Date(noonOf(date).getTime() + n * DAY_MS).toISOString().slice(0, 10);
}

// The instant 00:00 Pacific on a date: 07:00Z in daylight time, else 08:00Z.
function midnightUtc(date) {
  const daylight = new Date(`${date}T07:00:00Z`);
  return localDateOf(daylight, LEAGUE_ZONE) === date ? daylight : new Date(`${date}T08:00:00Z`);
}

// The window ending (inclusive) on lastDay: its UTC start and cell count.
export function weekWindow(lastDay) {
  const start = midnightUtc(addDays(lastDay, -7));
  const end = midnightUtc(addDays(lastDay, 1));
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
