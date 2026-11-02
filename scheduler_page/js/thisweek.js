// The this-week editor: today and the six days after, as dated columns pre-filled from the usual week, saved as
// dated overrides. A pure model (editorDates, columnLabel, prefill, toThisWeekFile) plus a thin DOM view.
import { DAY_CELLS, decodeCells, decodeDayCells, encodeDayCells, localDateOf, noonOf, weekdayOfDate } from "./week.js";
import { LinkExpired, checkDoc } from "./api.js";
import { expiredMessage } from "./messages.js";
import { el } from "./dom.js";
import { appBar } from "./appbar.js";
import { paintGrid, saveFailedMessage, touchHint, zoneSelect } from "./usual.js";

const FORMAT = "draftbot.scheduler.this_week";
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 86400000;
const RESET_FAILED = "Could not reset. Check your connection and press Reset again.";

// Today through today+6, as local dates in zone.
export function editorDates(now, zone) {
  const first = noonOf(localDateOf(now, zone)).getTime();
  return Array.from({ length: 7 }, (_, d) => new Date(first + d * DAY_MS).toISOString().slice(0, 10));
}

// "Tue 4 Nov" for a date; tabLabel is the short "Tue 4" the day tabs fit.
export function columnLabel(date) {
  const t = noonOf(date);
  return `${WEEKDAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
}
const tabLabel = (date) => columnLabel(date).split(" ").slice(0, 2).join(" ");

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

// The PUT body: every one of the seven dates, painted or not, so a date emptied this week stays empty.
export function toThisWeekFile(zone, dates, cells, alsoUsual) {
  const days = dates.map((date, d) => {
    const own = new Set();
    for (let h = 0; h < DAY_CELLS; h++) if (cells.has(d * DAY_CELLS + h)) own.add(h);
    return { date, cells: encodeDayCells(own) };
  });
  return { format: FORMAT, version: 1, zone, days, also_usual: alsoUsual };
}

// A page's way into the editor and back, like weekEditing. A save may also have written the usual week, so the
// page is drawn again with it re-read.
export function thisWeekEditing(root, api, { now, backLabel, dispose, show }) {
  return (usualDoc) => {
    dispose();
    root.classList.remove("wide");
    return mountThisWeek(root, api, {
      now, usualDoc, backLabel,
      onSaved: () => api.getUsual().catch(() => usualDoc).then((usual) => show(usual, "Saved.")),
      onBack: () => show(usualDoc, ""),
    });
  };
}

// deps: { now?: () => Date, usualDoc: usual-week doc | null, onSaved: (doc) => void, onBack: () => void,
//         backLabel: string, zones?: string[], defaultZone?: string }
// With a usual week the times are in its zone. Without one the player picks a zone and may keep the week as usual.
export async function mountThisWeek(root, api, deps) {
  const now = deps.now || (() => new Date());
  const usualDoc = deps.usualDoc;
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
  let dates = editorDates(now(), zone);

  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const touched = () => { status.textContent = ""; };
  const painter = paintGrid({
    labels: dates.map(columnLabel), tabLabels: dates.map(tabLabel), gridLabel: "This week, half-hour cells",
    cells: prefill(dates, usual, overrides), onChange: touched,
  });

  // No usual week: a zone to paint in, and the box that keeps this week as the usual one.
  const setup = [];
  let alsoUsual = null;
  if (!usual) {
    const { select, row } = zoneSelect(zone, deps.zones);
    // Today may be another date in the new zone; the painting stays where it is.
    select.addEventListener("change", () => {
      zone = select.value;
      dates = editorDates(now(), zone);
      painter.setLabels(dates.map(columnLabel), dates.map(tabLabel));
      touched();
    });
    setup.push(row);
    alsoUsual = el("input", { type: "checkbox", id: "alsousual" });
    alsoUsual.checked = true;
    alsoUsual.addEventListener("change", touched);
  }

  const save = el("button", { type: "button", class: "primary", text: "Save" });
  const reset = el("button", { type: "button", class: "secondary", text: "Reset to usual week" });
  const back = el("button", { type: "button", class: "secondary", text: deps.backLabel });
  back.addEventListener("click", () => deps.onBack());

  const busy = (on) => { save.disabled = on; reset.disabled = on; };
  const failed = (e, message) => { status.textContent = e instanceof LinkExpired ? expiredMessage() : message; };
  reset.addEventListener("click", async () => {
    busy(true);
    status.textContent = "Resetting…";
    try {
      await api.clearThisWeek();
      painter.setCells(prefill(dates, usual, []));
      status.textContent = usual ? "This week is back to your usual week." : "This week is cleared.";
    } catch (e) {
      failed(e, RESET_FAILED);
    } finally {
      busy(false);
    }
  });
  save.addEventListener("click", async () => {
    busy(true);
    status.textContent = "Saving…";
    try {
      const doc = toThisWeekFile(zone, dates, painter.cells(), Boolean(alsoUsual?.checked));
      await api.putThisWeek(doc);
      status.textContent = "Saved.";
      deps.onSaved(doc);
    } catch (e) {
      failed(e, saveFailedMessage(e));
    } finally {
      busy(false);
    }
  });

  root.replaceChildren(
    appBar("This week"),
    el("h1", { text: "When are you free this week?" }),
    el("p", { class: "lede", text: usual
      ? "This starts from your usual week. Paint or erase the half-hours that are different this week. Your usual week stays as it is."
      : "Drag or tap to paint the half-hours you can draft this week. Times are local to the zone below." }),
    ...setup,
    painter.tabs, touchHint(), painter.grid,
    ...(alsoUsual ? [el("label", { class: "alsousual", for: "alsousual" }, alsoUsual, el("span", { text: "Also keep this as my usual week" }))] : []),
    el("div", { class: "actions" }, save, reset, back),
    status,
  );
}
