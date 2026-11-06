// The shared heatmap: a 7-day window drawn in a viewer's zone. Pure layoutWindow plus a thin DOM mount.
import { CELLS, CELL_MINUTES, addCells } from "./week.js";
import { el } from "./dom.js";

const partsFormatters = new Map();
function partsOf(date, zone) {
  let f = partsFormatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    partsFormatters.set(zone, f);
  }
  const p = {};
  for (const x of f.formatToParts(date)) p[x.type] = x.value;
  return p;
}

// The league's deadline clock. A round runs Monday to Sunday, and every match must be finished by midnight Pacific at
// the end of Sunday (3 AM Eastern on Monday), per the league rules page; so its days are counted in Pacific time.
export const LEAGUE_ZONE = "America/Los_Angeles";

// One column per local date, in order; each cell is a window index j with its local minutes-of-day.
// A DST day simply has 46 or 50 cells; nothing assumes 48 per day.
// Each cell also carries its league week, by the league's clock wherever the viewer is: kind is "due" on the league's
// Sunday (the last day to get that round's matches finished), else "wk0" in the week the window opens in and "wk1" from the
// next league Monday on. cutoff marks the last cell before results are due. A window opening in Sunday's last half-hour
// of a spring-forward week ends one cell past a second Monday; that cell is tinted as the next week too.
export function layoutWindow(startUtc, zone) {
  const days = [];
  let week = 0;
  let leagueDay = null;
  for (let j = 0; j < CELLS; j++) {
    const at = addCells(startUtc, j);
    const p = partsOf(at, zone);
    const date = `${p.year}-${p.month}-${p.day}`;
    let day = days[days.length - 1];
    if (!day || day.date !== date) {
      day = { date, label: `${p.weekday} ${Number(p.day)}`, cells: [] };
      days.push(day);
    }
    const league = partsOf(at, LEAGUE_ZONE).weekday;
    if (leagueDay === "Sun" && league === "Mon") week++;
    leagueDay = league;
    const due = league === "Sun";
    const cutoff = due && partsOf(addCells(startUtc, j + 1), LEAGUE_ZONE).weekday === "Mon";
    day.cells.push({ j, minutes: Number(p.hour) * 60 + Number(p.minute), kind: due ? "due" : `wk${Math.min(week, 1)}`, cutoff });
  }
  return days;
}

const labelFormatters = new Map();
// "Wed 21 Oct, 7:00 PM" for an instant, in zone. Every view labels a time with this.
export function formatTime(date, zone) {
  let f = labelFormatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hour12: true,
    });
    labelFormatters.set(zone, f);
  }
  const p = {};
  for (const part of f.formatToParts(date)) p[part.type] = part.value;
  return `${p.weekday} ${p.day} ${p.month}, ${p.hour}:${p.minute} ${p.dayPeriod.toUpperCase()}`;
}

// The label of window cell j, in zone.
export const formatCell = (startUtc, zone, j) => formatTime(addCells(startUtc, j), zone);

// The window as a grid's columns (see weekColumns in usual.js), one per local date, keyed by window cell: the
// heatmap and both week editors on a matchup draw these, so all three show the same days and times. A partial first
// day starts part-way down (start, in half-hours after local midnight); date is the column's "YYYY-MM-DD". Each cell's
// league kind and cutoff (see layoutWindow) come along for the grid to tint, and the column's kind is the one most of
// its cells have, for its name and tab.
export const windowColumns = (startUtc, zone) => layoutWindow(startUtc, zone).map((d) => ({
  label: d.label, date: d.date, kind: mostCommon(d.cells.map((c) => c.kind)), start: Math.floor(d.cells[0].minutes / CELL_MINUTES),
  cells: d.cells.map((c) => ({ key: c.j, label: formatCell(startUtc, zone, c.j), kind: c.kind, cutoff: c.cutoff })),
}));

function mostCommon(xs) {
  const counts = new Map();
  for (const x of xs) counts.set(x, (counts.get(x) ?? 0) + 1);
  return [...counts].reduce((a, b) => (b[1] > a[1] ? b : a))[0];
}

// "7 PM" for hour h (0-23): the hour column of every grid.
export const hourLabel = (h) => `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "AM" : "PM"}`;

// Every row's classes, so an hour reads as one block across the grid and lines up with its label: "hr" opens an
// hour and "half" closes it (the CSS spaces hours apart and pairs their half-hours), and "alt" shades every other hour.
export const rowClass = (row) => `${row % 2 ? "half" : "hr"}${Math.floor(row / 2) % 2 ? " alt" : ""}`;

// The frame both grids draw from a layout's columns (see windowColumns): day tabs (shown under 480px, where one day
// fills the screen), the hour column, and a column per layout column with its name and the padding down to its first
// row. makeCell(cell, rowClass) builds each cell's element, which carries its row's classes; attrs are the grid's
// own. Cells and columns with a league kind (windowColumns) are tinted by it, and the cutoff cell is underlined: those
// classes ride along with the row's. It opens on the tab of the column dated openDate, else the first.
export function gridFrame(columns, attrs, makeCell, openDate) {
  const tabs = el("div", { class: "daytabs", role: "tablist", "aria-label": "Day" });
  const grid = el("div", attrs);
  const kindClass = (x) => (x.kind ? ` ${x.kind}${x.cutoff ? " cutoff" : ""}` : "");
  const tabButtons = columns.map((c, i) => {
    const b = el("button", { type: "button", role: "tab", text: c.label, class: kindClass(c).trim() });
    b.addEventListener("click", () => setDay(i));
    tabs.append(b);
    return b;
  });
  let active = 0;
  const setDay = (day) => {
    active = day;
    grid.dataset.active = String(day);
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(i === day)));
  };
  const hours = el("div", { class: "col hours", "aria-hidden": "true" });
  for (let h = 0; h < 48; h++) hours.append(el("div", { class: `hour ${rowClass(h)}`, text: h % 2 === 0 ? hourLabel(h / 2) : "" }));
  grid.append(hours);
  columns.forEach((c, d) => {
    const col = el("div", { class: `col day${kindClass(c)}`, "data-day": String(d), role: "row" });
    col.append(el("div", { class: "dayname", text: c.label, "aria-hidden": "true" }));
    for (let g = 0; g < c.start; g++) col.append(el("div", { class: `gap ${rowClass(g)}`, "aria-hidden": "true" }));
    c.cells.forEach((cell, i) => col.append(makeCell(cell, rowClass(c.start + i) + kindClass(cell))));
    grid.append(col);
  });
  setDay(Math.max(0, columns.findIndex((c) => c.date === openDate)));
  return { tabs, grid, setDay, openDate: () => columns[active].date };
}

// A legend key and its words, kept together when the legend wraps on a narrow screen.
export const legendKey = (kind, text) => el("span", { class: "keyitem" }, el("i", { class: `key ${kind}` }), el("span", { text }));

// The legend keys for a window grid's league tints (see gridFrame): the due key is the warning.
export const weekKeys = () => [
  legendKey("wk0", "this week"),
  legendKey("due", "Sunday: matches due by midnight PT (3 AM ET Mon)"),
  legendKey("wk1", "next week"),
];

// opts: { startUtc, zone, cellClass(j) -> string, cellLabel(j) -> string, onPick?(j), onHover?(j | null, byMouse), openDate? }
// onPick makes cells buttons (tap or Enter); onHover(j, byMouse) follows the pointer and keyboard focus, and onHover(null)
// fires when a mouse leaves (byMouse says whether a mouse is over the cell now, as opposed to a finger or the keyboard).
// openDate is the day tab to open on (see gridFrame): a page drawing its heatmap again passes the old one's openDate(),
// so a narrow screen stays on the day it was showing.
export function mountHeatmap(container, opts) {
  const { startUtc, zone, cellClass, cellLabel, cellText, onPick, onHover, openDate } = opts;
  const nodes = new Map();
  // Each cell's row classes, kept apart from cellClass so a redraw never drops them.
  const rows = new Map();
  const frame = gridFrame(windowColumns(startUtc, zone), {
    class: cellText ? "grid heat counted" : "grid heat", role: "grid",
    "aria-label": `Next 7 days, half-hour cells, times in ${zone.replaceAll("_", " ")}`,
  }, ({ key: j }, row) => {
    const n = el(onPick ? "button" : "div", { class: `hcell ${row}`, role: "gridcell", "data-j": String(j) });
    if (onPick) { n.type = "button"; n.addEventListener("click", () => onPick(j)); }
    nodes.set(j, n);
    rows.set(j, row);
    return n;
  }, openDate);
  const { tabs, grid } = frame;

  // Touch the DOM only for cells whose look or words changed: 336 cells redraw on every tap.
  const update = () => {
    for (const [j, n] of nodes) {
      const cls = `hcell ${rows.get(j)} ${cellClass(j)}`.trim();
      if (n.className !== cls) n.className = cls;
      const label = cellLabel(j);
      if (n.title !== label) { n.title = label; n.setAttribute("aria-label", label); }
      // Shown text only; the aria-label above is what a screen reader reads.
      if (cellText) { const t = cellText(j); if (n.textContent !== t) n.textContent = t; }
    }
  };

  if (onHover) {
    const hovered = (e) => { const c = e.target.closest?.(".hcell[data-j]"); if (c) onHover(Number(c.dataset.j), e.type === "pointerover" && e.pointerType === "mouse"); };
    grid.addEventListener("pointerover", hovered);
    grid.addEventListener("focusin", hovered);
    // A finger lifting also "leaves": only a mouse leaving clears the readout, so a tap stays shown.
    grid.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") onHover(null); });
  }

  container.replaceChildren(tabs, grid);
  update();
  return { update, openDate: frame.openDate };
}
