// Week bitmaps and the usual-week -> window conversion. Plain ES module, no dependencies.
export const CELLS = 336;
export const CELL_MINUTES = 30;
export const CELL_MS = CELL_MINUTES * 60000;
const BYTES = CELLS / 8;
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

// Window cell j is free iff the local time of startUtc + 30 min * j falls in a painted usual-week cell.
export function windowCells(zone, usual, startUtc) {
  const out = new Set();
  for (let j = 0; j < CELLS; j++) {
    if (usual.has(usualCellOf(addCells(startUtc, j), zone))) out.add(j);
  }
  return out;
}

// Half-open [start, end) runs of consecutive cells, sorted.
export function mergeBlocks(cells) {
  const blocks = [];
  for (const i of [...cells].sort((a, b) => a - b)) {
    const last = blocks[blocks.length - 1];
    if (last && last[1] === i) last[1] = i + 1;
    else blocks.push([i, i + 1]);
  }
  return blocks;
}

// The instant j half-hour cells after startUtc.
export const addCells = (startUtc, j) => new Date(startUtc.getTime() + j * CELL_MS);

// Window times of a match file (5): cell j is the half hour at window.start_utc + 30 min * j.
export const startMs = (shared) => new Date(shared.window.start_utc).getTime();
export const instantOf = (shared, j) => new Date(startMs(shared) + j * CELL_MS);

// An ISO instant without milliseconds, as the bot's files carry it.
export const isoSeconds = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
