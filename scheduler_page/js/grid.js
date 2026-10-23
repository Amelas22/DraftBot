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

// "7 PM" for hour h (0-23): the hour column of every grid.
export const hourLabel = (h) => `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "AM" : "PM"}`;

// opts: { startUtc, zone, cellClass(j) -> string, cellLabel(j) -> string, onPick?(j) }
export function mountHeatmap(container, opts) {
  const { startUtc, zone, cellClass, cellLabel, onPick } = opts;
  const days = layoutWindow(startUtc, zone);

  const tabs = el("div", { class: "daytabs", role: "tablist", "aria-label": "Day" });
  const tabButtons = days.map((d, i) => {
    const b = el("button", { type: "button", role: "tab", text: d.label });
    b.addEventListener("click", () => setDay(i));
    tabs.append(b);
    return b;
  });
  const setDay = (day) => {
    grid.dataset.active = String(day);
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(i === day)));
  };

  const grid = el("div", { class: "grid heat", role: "grid", "aria-label": `Next 7 days, half-hour cells, times in ${zone.replaceAll("_", " ")}` });
  const hours = el("div", { class: "col hours", "aria-hidden": "true" });
  for (let h = 0; h < 48; h++) hours.append(el("div", { class: "hour", text: h % 2 === 0 ? hourLabel(h / 2) : "" }));
  grid.append(hours);

  const nodes = new Map();
  days.forEach((d, i) => {
    const col = el("div", { class: "col day", "data-day": String(i), role: "row" });
    col.append(el("div", { class: "dayname", text: d.label, "aria-hidden": "true" }));
    // A partial first day starts part-way down: pad to its local time.
    for (let g = 0; g < Math.floor(d.cells[0].minutes / CELL_MINUTES); g++) col.append(el("div", { class: "hcell gap", "aria-hidden": "true" }));
    for (const c of d.cells) {
      const n = el(onPick ? "button" : "div", { class: "hcell", role: "gridcell" });
      if (onPick) { n.type = "button"; n.addEventListener("click", () => onPick(c.j)); }
      nodes.set(c.j, n);
      col.append(n);
    }
    grid.append(col);
  });

  const update = () => {
    for (const [j, n] of nodes) {
      n.className = `hcell ${cellClass(j)}`.trim();
      const label = cellLabel(j);
      n.setAttribute("aria-label", label);
      n.title = label;
    }
  };

  container.replaceChildren(tabs, grid);
  setDay(0);
  update();
  return { update };
}
