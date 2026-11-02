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

// One column per local date, in order; each cell is a window index j with its local minutes-of-day.
// A DST day simply has 46 or 50 cells; nothing assumes 48 per day.
export function layoutWindow(startUtc, zone) {
  const days = [];
  for (let j = 0; j < CELLS; j++) {
    const p = partsOf(addCells(startUtc, j), zone);
    const date = `${p.year}-${p.month}-${p.day}`;
    let day = days[days.length - 1];
    if (!day || day.date !== date) {
      day = { date, label: `${p.weekday} ${Number(p.day)}`, cells: [] };
      days.push(day);
    }
    day.cells.push({ j, minutes: Number(p.hour) * 60 + Number(p.minute) });
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
// day starts part-way down (start, in half-hours after local midnight); date is the column's "YYYY-MM-DD".
export const windowColumns = (startUtc, zone) => layoutWindow(startUtc, zone).map((d) => ({
  label: d.label, date: d.date, start: Math.floor(d.cells[0].minutes / CELL_MINUTES),
  cells: d.cells.map((c) => ({ key: c.j, label: formatCell(startUtc, zone, c.j) })),
}));

// "7 PM" for hour h (0-23): the hour column of every grid.
export const hourLabel = (h) => `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "AM" : "PM"}`;

// The frame both grids draw from a layout's columns (see windowColumns): day tabs (shown under 480px, where one day
// fills the screen), the hour column, and a column per layout column with its name and the padding down to its first
// row. makeCell(cell) builds each cell's element; attrs are the grid's own. It opens on the first day's tab.
export function gridFrame(columns, attrs, makeCell) {
  const tabs = el("div", { class: "daytabs", role: "tablist", "aria-label": "Day" });
  const grid = el("div", attrs);
  const tabButtons = columns.map((c, i) => {
    const b = el("button", { type: "button", role: "tab", text: c.label });
    b.addEventListener("click", () => setDay(i));
    tabs.append(b);
    return b;
  });
  const setDay = (day) => {
    grid.dataset.active = String(day);
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(i === day)));
  };
  const hours = el("div", { class: "col hours", "aria-hidden": "true" });
  for (let h = 0; h < 48; h++) hours.append(el("div", { class: "hour", text: h % 2 === 0 ? hourLabel(h / 2) : "" }));
  grid.append(hours);
  columns.forEach((c, d) => {
    const col = el("div", { class: "col day", "data-day": String(d), role: "row" });
    col.append(el("div", { class: "dayname", text: c.label, "aria-hidden": "true" }));
    for (let g = 0; g < c.start; g++) col.append(el("div", { class: "gap", "aria-hidden": "true" }));
    for (const cell of c.cells) col.append(makeCell(cell));
    grid.append(col);
  });
  setDay(0);
  return { tabs, grid, setDay };
}

// opts: { startUtc, zone, cellClass(j) -> string, cellLabel(j) -> string, onPick?(j), onHover?(j | null, byMouse) }
// onPick makes cells buttons (tap or Enter); onHover(j, byMouse) follows the pointer and keyboard focus, and onHover(null)
// fires when a mouse leaves (byMouse says whether a mouse is over the cell now, as opposed to a finger or the keyboard).
export function mountHeatmap(container, opts) {
  const { startUtc, zone, cellClass, cellLabel, cellText, onPick, onHover } = opts;
  const nodes = new Map();
  const { tabs, grid } = gridFrame(windowColumns(startUtc, zone), {
    class: cellText ? "grid heat counted" : "grid heat", role: "grid",
    "aria-label": `Next 7 days, half-hour cells, times in ${zone.replaceAll("_", " ")}`,
  }, ({ key: j }) => {
    const n = el(onPick ? "button" : "div", { class: "hcell", role: "gridcell", "data-j": String(j) });
    if (onPick) { n.type = "button"; n.addEventListener("click", () => onPick(j)); }
    nodes.set(j, n);
    return n;
  });

  // Touch the DOM only for cells whose look or words changed: 336 cells redraw on every tap.
  const update = () => {
    for (const [j, n] of nodes) {
      const cls = `hcell ${cellClass(j)}`.trim();
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
  return { update };
}
