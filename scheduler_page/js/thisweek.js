// The this-week editor on a matchup: the matchup's window (the same days and times as its heatmap), pre-filled from
// the usual week and saved as dated overrides as the player paints. Cells already past are shown but can't be
// changed. A pure model (prefill, shownCells, withPainted, pastCells, toThisWeekFile) plus a thin DOM view.
import {
  CELLS, DAY_CELLS, addCells, dayCellOf, decodeCells, decodeDayCells, encodeDayCells, localDateOf, usualWithWindow,
  weekdayOfDate,
} from "./week.js";
import { LinkExpired, checkDoc } from "./api.js";
import { expiredMessage } from "./messages.js";
import { el } from "./dom.js";
import { windowColumns, zoneName } from "./grid.js";
import { autosaver, paintGrid, paintLede, saveFailedMessage, toFile, touchHint, zoneSelect } from "./usual.js";

const FORMAT = "draftbot.scheduler.this_week";
const RESET_FAILED = "Could not reset. Check your connection and press Reset again.";
const ALL_PAST = "These days are over, so they can't be changed.";
const ZONE_FAILED = "Could not change the time zone. Check your connection and try again.";

// The grid's cells: column d shows the override saved for dates[d], else the usual week's cells for that weekday.
// usual: { zone, cells: Set<0..335> } | null; overrides: [{ date, cells: Set<0..47> }].
export function prefill(dates, usual, overrides) {
  const out = new Set();
  dates.forEach((date, d) => {
    const o = overrides.find((x) => x.date === date);
    for (let h = 0; h < DAY_CELLS; h++) {
      const on = o ? o.cells.has(h) : Boolean(usual?.cells.has(weekdayOfDate(date) * DAY_CELLS + h));
      if (on) out.add(d * DAY_CELLS + h);
    }
  });
  return out;
}

// Where window cell j falls among the dates' whole days, as prefill keys them: date index * 48 + half-hour, in zone.
function dayKey(dates, startUtc, zone, j) {
  const t = addCells(startUtc, j);
  return dates.indexOf(localDateOf(t, zone)) * DAY_CELLS + dayCellOf(t, zone);
}

// The window cells (of count) that whole days (prefill's keys) have on: what the grid shows.
export function shownCells(dates, days, startUtc, zone, count = CELLS) {
  const out = new Set();
  for (let j = 0; j < count; j++) if (days.has(dayKey(dates, startUtc, zone, j))) out.add(j);
  return out;
}

// Whole days with the window's cells set or cleared as painted has them. The hours of the first and last date outside
// the window keep what they had, so saving a day never wipes the part of it the grid doesn't show.
export function withPainted(dates, days, painted, startUtc, zone, count = CELLS) {
  const out = new Set(days);
  for (let j = 0; j < count; j++) {
    const k = dayKey(dates, startUtc, zone, j);
    if (painted.has(j)) out.add(k);
    else out.delete(k);
  }
  return out;
}

// The window cells (of count) that have started by now: the past, which the editor shows but never changes.
export function pastCells(startUtc, count, now) {
  const out = new Set();
  for (let j = 0; j < count && addCells(startUtc, j) <= now; j++) out.add(j);
  return out;
}

// The PUT body: every one of the dates, painted or not, so a date emptied this week stays empty.
export function toThisWeekFile(zone, dates, cells) {
  const days = dates.map((date, d) => {
    const own = new Set();
    for (let h = 0; h < DAY_CELLS; h++) if (cells.has(d * DAY_CELLS + h)) own.add(h);
    return { date, cells: encodeDayCells(own) };
  });
  return { format: FORMAT, version: 1, zone, days };
}

// deps: { startUtc: Date, cells?: number, shape?: string, now?: () => Date, usualDoc: usual-week doc | null,
//         onSaved?: (doc) => void, onKeptAsUsual?: (doc) => void, zones?: string[], defaultZone?: string,
//         saveDelay?: number }
// Drawn under the matchup's card, so it has no app bar or heading of its own; its columns are the window of cells
// half-hours (in shape) that starts at startUtc. It reads, saves and resets only the window's dates, and leaves cells
// that have started by now() as they are; when every cell has, it says so and never saves. With a usual week the
// times are in its zone. Without one the player picks a zone, and Save as my usual week turns the painting into their
// usual week and drops this week's changes from today on, then calls onKeptAsUsual(doc). It saves saveDelay ms after
// the last change and hands each saved file to onSaved. Returns { flush } (see autosaver), or nothing on an expired
// link.
export async function mountThisWeek(root, api, deps) {
  const { startUtc, usualDoc, cells: count = CELLS, shape } = deps;
  const now = deps.now || (() => new Date());
  const usual = usualDoc?.zone && usualDoc.cells ? { zone: usualDoc.zone, cells: decodeCells(usualDoc.cells) } : null;
  const locked = pastCells(startUtc, count, now());
  const allPast = locked.size === count;

  // The window's columns and their dates in zone, the saved overrides for those dates, and the whole days behind them
  // (see withPainted). The overrides are read for the dates in view, and read again when a new zone shows other dates.
  let zone = usual?.zone || deps.defaultZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  let columns;
  let dates;
  let days;
  let overrides = [];
  let read = null;
  const lay = () => {
    columns = windowColumns(startUtc, zone, count, shape);
    dates = columns.map((c) => c.date);
  };
  // Reads the overrides for ds (a layout's dates) unless the last read covered them.
  const readDates = async (ds) => {
    if (read && read.from <= ds[0] && ds.at(-1) <= read.to) return null;
    const range = { from: ds[0], to: ds.at(-1) };
    const saved = checkDoc(await api.getThisWeek(range), FORMAT);
    overrides = saved.days.map((d) => ({ date: d.date, cells: decodeDayCells(d.cells) }));
    read = range;
    return saved;
  };

  root.replaceChildren(el("p", { class: "status", text: "Loading your week…" }));
  try {
    lay();
    const saved = await readDates(dates);
    // Without a usual week the times are in the zone this week was last saved in, which may show other dates.
    if (!usual && saved.zone && saved.zone !== zone) {
      zone = saved.zone;
      lay();
      await readDates(dates);
    }
  } catch (e) {
    if (e instanceof LinkExpired) { root.replaceChildren(el("p", { class: "notice", text: expiredMessage() })); return; }
    throw e;
  }
  days = prefill(dates, usual, overrides);
  // The dates before today (in zone) are over: a save leaves them out, and a reset keeps their changes.
  const today = () => localDateOf(now(), zone);

  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  // painter is set below, before any change can fire. Every date not yet over is sent, so a date painted back to
  // the usual week drops its old change.
  const write = async () => {
    days = withPainted(dates, days, painter.cells(), startUtc, zone, count);
    const file = toThisWeekFile(zone, dates, days);
    const doc = { ...file, days: file.days.filter((d) => d.date >= today()) };
    await api.putThisWeek(doc);
    deps.onSaved?.(doc);
  };
  const auto = autosaver(write, (text) => { status.textContent = text; }, deps.saveDelay);
  const touched = allPast ? () => {} : () => { status.textContent = ""; auto.touch(); };
  let painter;
  const paintBox = el("div");
  const draw = (cells) => {
    painter = paintGrid({ columns, gridLabel: "This week, half-hour cells", cells, onChange: touched, locked });
    paintBox.replaceChildren(painter.tabs, touchHint(), painter.grid);
  };
  draw(shownCells(dates, days, startUtc, zone, count));

  // No usual week: a zone to paint in, and (unless it's all over) the button that keeps this week as the usual one.
  const setup = [];
  let keep = null;
  if (!usual) {
    const { select, row } = zoneSelect(zone, deps.zones);
    // The painting stays on the same moments; the window's dates and hours are drawn again in the new zone.
    // The new zone takes over only once its dates are read, so a save never writes a date it hasn't read; until then
    // (and after a failed read, which puts the old zone back) saves go on in the old zone.
    select.addEventListener("change", async () => {
      const next = select.value;
      const nextColumns = windowColumns(startUtc, next, count, shape);
      const nextDates = nextColumns.map((c) => c.date);
      try {
        await readDates(nextDates);
      } catch (e) {
        select.value = zone;
        status.textContent = e instanceof LinkExpired ? expiredMessage() : ZONE_FAILED;
        return;
      }
      const painted = painter.cells();
      zone = next;
      columns = nextColumns;
      dates = nextDates;
      days = prefill(dates, usual, overrides);
      draw(painted);
      touched();
    });
    setup.push(row);
  }
  if (!usual && !allPast) {
    keep = el("button", { type: "button", class: "primary", text: "Save as my usual week" });
    // The painted window becomes the usual week by weekday and time, as the Usual week editor maps it; this week's
    // changes then match it, so they are dropped rather than left to shadow later edits to the usual week.
    // The grid is inert meanwhile, so no stroke lands between reading the painting and dropping this week.
    keep.addEventListener("click", async () => {
      keep.disabled = true;
      paintBox.inert = true;
      auto.cancel();
      await auto.flush();
      status.textContent = "Saving…";
      try {
        // Over a window longer than a week, the later of two same-weekday days wins.
        const doc = toFile(zone, usualWithWindow(new Set(), painter.cells(), zone, startUtc, count), now());
        await api.putUsual(doc);
        // Every change from today on, not just this window's: the server keeps the past.
        await api.clearThisWeek();
        // A save that failed in the flush above is still pending; it would put this week's changes back.
        auto.cancel();
        deps.onKeptAsUsual?.(doc);
      } catch (e) {
        status.textContent = e instanceof LinkExpired ? expiredMessage() : saveFailedMessage(e);
        keep.disabled = false;
        paintBox.inert = false;
      }
    });
  }

  const reset = el("button", { type: "button", class: "secondary", text: "Reset to usual week" });
  reset.addEventListener("click", async () => {
    reset.disabled = true;
    // Drop what is waiting to save, and let a save already sent land before the reset clears it.
    auto.cancel();
    await auto.flush();
    status.textContent = "Resetting…";
    try {
      await api.clearThisWeek({ from: dates[0], to: dates.at(-1) });
      overrides = overrides.filter((o) => o.date < today());
      days = prefill(dates, usual, overrides);
      painter.setCells(shownCells(dates, days, startUtc, zone, count));
      status.textContent = usual ? "This week is back to your usual week." : "This week is cleared.";
    } catch (e) {
      status.textContent = e instanceof LinkExpired ? expiredMessage() : RESET_FAILED;
    } finally {
      reset.disabled = false;
    }
  });

  root.replaceChildren(
    allPast ? el("p", { class: "notice", text: ALL_PAST }) : paintLede(usual
      ? `This starts from your usual week. Paint or erase the half-hours that are different this week. Your usual week stays as it is. Times are in ${zoneName(zone)}, your usual week's zone.`
      : "A draft and its matches take a few hours. Drag or tap to paint this week. Times are local to the zone below. To keep them for every week, press Save as my usual week."),
    ...setup,
    paintBox,
    ...(allPast ? [] : [el("div", { class: "actions" }, ...(keep ? [keep] : []), reset)]),
    status,
  );
  return { flush: auto.flush };
}
