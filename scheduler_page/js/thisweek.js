// The this-week editor on a matchup: the matchup's window (the same days and times as its heatmap), pre-filled from
// the usual week and saved as dated overrides as the player paints. A pure model (prefill, shownCells, withPainted,
// toThisWeekFile) plus a thin DOM view.
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

// The window cells that whole days (prefill's keys) have on: what the grid shows.
export function shownCells(dates, days, startUtc, zone) {
  const out = new Set();
  for (let j = 0; j < CELLS; j++) if (days.has(dayKey(dates, startUtc, zone, j))) out.add(j);
  return out;
}

// Whole days with the window's cells set or cleared as painted has them. The hours of the first and last date outside
// the window keep what they had, so saving a day never wipes the part of it the grid doesn't show.
export function withPainted(dates, days, painted, startUtc, zone) {
  const out = new Set(days);
  for (let j = 0; j < CELLS; j++) {
    const k = dayKey(dates, startUtc, zone, j);
    if (painted.has(j)) out.add(k);
    else out.delete(k);
  }
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

// deps: { startUtc: Date, usualDoc: usual-week doc | null, onSaved?: (doc) => void, onKeptAsUsual?: (doc) => void,
//         zones?: string[], defaultZone?: string, saveDelay?: number }
// Drawn under the matchup's card, so it has no app bar or heading of its own; its columns are the window that starts
// at startUtc. With a usual week the times are in its zone. Without one the player picks a zone, and Save as my usual
// week turns the painting into their usual week and drops this week's changes, then calls onKeptAsUsual(doc). It
// saves saveDelay ms after the last change and hands each saved file to onSaved. Returns { flush } (see autosaver),
// or nothing on an expired link.
export async function mountThisWeek(root, api, deps) {
  const { startUtc, usualDoc } = deps;
  const usual = usualDoc?.zone && usualDoc.cells ? { zone: usualDoc.zone, cells: decodeCells(usualDoc.cells) } : null;

  root.replaceChildren(el("p", { class: "status", text: "Loading your week…" }));
  let saved;
  try {
    saved = checkDoc(await api.getThisWeek(), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) { root.replaceChildren(el("p", { class: "notice", text: expiredMessage() })); return; }
    throw e;
  }
  const overrides = saved.days.map((d) => ({ date: d.date, cells: decodeDayCells(d.cells) }));

  let zone = usual?.zone || saved.zone || deps.defaultZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  // The window's columns and their dates in zone, and the whole days behind them (see withPainted).
  let columns;
  let dates;
  let days;
  const layOut = () => {
    columns = windowColumns(startUtc, zone);
    dates = columns.map((c) => c.date);
    days = prefill(dates, usual, overrides);
  };
  layOut();

  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  // painter is set below, before any change can fire.
  const write = async () => {
    days = withPainted(dates, days, painter.cells(), startUtc, zone);
    const doc = toThisWeekFile(zone, dates, days);
    await api.putThisWeek(doc);
    deps.onSaved?.(doc);
  };
  const auto = autosaver(write, (text) => { status.textContent = text; }, deps.saveDelay);
  const touched = () => { status.textContent = ""; auto.touch(); };
  let painter;
  const paintBox = el("div");
  const draw = (cells) => {
    painter = paintGrid({ columns, gridLabel: "This week, half-hour cells", cells, onChange: touched });
    paintBox.replaceChildren(painter.tabs, touchHint(), painter.grid);
  };
  draw(shownCells(dates, days, startUtc, zone));

  // No usual week: a zone to paint in, and the button that keeps this week as the usual one.
  const setup = [];
  let keep = null;
  if (!usual) {
    const { select, row } = zoneSelect(zone, deps.zones);
    // The painting stays on the same moments; the window's dates and hours are drawn again in the new zone.
    select.addEventListener("change", () => {
      const painted = painter.cells();
      zone = select.value;
      layOut();
      draw(painted);
      touched();
    });
    setup.push(row);
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
        const doc = toFile(zone, usualWithWindow(new Set(), painter.cells(), zone, startUtc), new Date());
        await api.putUsual(doc);
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
      await api.clearThisWeek();
      days = prefill(dates, usual, []);
      painter.setCells(shownCells(dates, days, startUtc, zone));
      status.textContent = usual ? "This week is back to your usual week." : "This week is cleared.";
    } catch (e) {
      status.textContent = e instanceof LinkExpired ? expiredMessage() : RESET_FAILED;
    } finally {
      reset.disabled = false;
    }
  });

  root.replaceChildren(
    paintLede(usual
      ? `This starts from your usual week. Paint or erase the half-hours that are different this week. Your usual week stays as it is. Times are in ${zoneName(zone)}, your usual week's zone.`
      : "A draft and its matches take a few hours. Drag or tap to paint this week. Times are local to the zone below. To keep them for every week, press Save as my usual week."),
    ...setup,
    paintBox,
    el("div", { class: "actions" }, ...(keep ? [keep] : []), reset),
    status,
  );
  return { flush: auto.flush };
}
