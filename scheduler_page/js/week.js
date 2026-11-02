// Week bitmaps and the usual-week -> window conversion. Plain ES module, no dependencies.
export const CELLS = 336;
export const CELL_MINUTES = 30;
export const CELL_MS = CELL_MINUTES * 60000;
export const DAY_CELLS = 48;
const BYTES = CELLS / 8;
const DAY_BYTES = DAY_CELLS / 8;
const WEEKDAYS = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };

export function decodeCells(b64) {
  const bin = atob(b64);
  if (bin.length !== BYTES) throw new Error(`expected ${BYTES} bytes, got ${bin.length}`);
  const cells = new Set();
  for (let i = 0; i < CELLS; i++) {
    if (bin.charCodeAt(i >> 3) & (0x80 >> (i % 8))) cells.add(i);
  }
  return cells;
}

export function encodeCells(cells) {
  const bytes = new Uint8Array(BYTES);
  for (const i of cells) bytes[i >> 3] |= 0x80 >> (i % 8);
  return btoa(String.fromCharCode(...bytes));
}

export function decodeDayCells(b64) {
  const bin = atob(b64);
  if (bin.length !== DAY_BYTES) throw new Error(`expected ${DAY_BYTES} bytes, got ${bin.length}`);
  const cells = new Set();
  for (let i = 0; i < DAY_CELLS; i++) {
    if (bin.charCodeAt(i >> 3) & (0x80 >> (i % 8))) cells.add(i);
  }
  return cells;
}

export function encodeDayCells(cells) {
  const bytes = new Uint8Array(DAY_BYTES);
  for (const i of cells) bytes[i >> 3] |= 0x80 >> (i % 8);
  return btoa(String.fromCharCode(...bytes));
}

const formatters = new Map();
function formatterFor(zone) {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    formatters.set(zone, f);
  }
  return f;
}

// The usual-week cell (Monday 00:00 local = 0) that a real instant falls in.
export function usualCellOf(date, zone) {
  const parts = {};
  for (const p of formatterFor(zone).formatToParts(date)) parts[p.type] = p.value;
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  return WEEKDAYS[parts.weekday] * 48 + Math.floor(minutes / CELL_MINUTES);
}

const dateFormatters = new Map();

// The local calendar date, "YYYY-MM-DD", that a real instant falls on in a zone.
export function localDateOf(date, zone) {
  let f = dateFormatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" });
    dateFormatters.set(zone, f);
  }
  const parts = {};
  for (const p of f.formatToParts(date)) parts[p.type] = p.value;
  return `${parts.year}-${parts.month}-${parts.day}`;
}

// The usual-week day (Monday = 0) a "YYYY-MM-DD" date falls on.
export const weekdayOfDate = (date) => (new Date(`${date}T12:00:00Z`).getUTCDay() + 6) % 7;

// The half-hour of the local day (0..47) that a real instant falls in.
export const dayCellOf = (date, zone) => usualCellOf(date, zone) % 48;

// Window cell j is free iff the local time of startUtc + 30 min * j falls in a painted usual-week cell.
export function windowCells(zone, usual, startUtc) {
  const out = new Set();
  for (let j = 0; j < CELLS; j++) {
    if (usual.has(usualCellOf(addCells(startUtc, j), zone))) out.add(j);
  }
  return out;
}

// A dated override replaces the usual week for its local day; with no usual week, other days are busy.
export function effectiveWindowCells(usual, overrides, startUtc) {
  const base = usual ? windowCells(usual.zone, usual.cells, startUtc) : new Set();
  if (overrides.length === 0) return base;
  const out = new Set();
  for (let j = 0; j < CELLS; j++) {
    const t = addCells(startUtc, j);
    const o = overrides.find((x) => localDateOf(t, x.zone) === x.date);
    if (o ? o.cells.has(dayCellOf(t, o.zone)) : base.has(j)) out.add(j);
  }
  return out;
}

// The instant j half-hour cells after startUtc.
export const addCells = (startUtc, j) => new Date(startUtc.getTime() + j * CELL_MS);

// An ISO instant without milliseconds, as the bot's files carry it.
export const isoSeconds = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
