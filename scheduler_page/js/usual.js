// The usual-week editor: a pure model (applyStroke, toFile, loadWeek) plus a thin DOM view (mountUsual).
import { decodeCells, encodeCells, isoSeconds, usualWithWindow, windowCells } from "./week.js";
import { LinkExpired, checkDoc } from "./api.js";
import { expiredMessage, NOT_ENABLED } from "./messages.js";
import { el } from "./dom.js";
import { appBar } from "./appbar.js";
import { gridFrame, hourLabel, windowColumns, zoneName } from "./grid.js";

const FORMAT = "draftbot.scheduler.usual_week";
const PER_DAY = 48;
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Fill the rectangle between two cells of a paint grid (the same columns range x the same rows range), by key.
// geo (from gridGeometry) is the grid's layout; the default is the usual Mon-Sun week.
export function applyStroke(cells, from, to, paint, geo = WEEK_GEOMETRY) {
  const out = new Set(cells);
  const [x0, y0] = geo.pos.get(from);
  const [x1, y1] = geo.pos.get(to);
  for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++) {
    for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) {
      const k = geo.keyAt(x, y);
      if (k === undefined) continue;
      if (paint) out.add(k);
      else out.delete(k);
    }
  }
  return out;
}

export function toFile(zone, cells, now) {
  return {
    format: FORMAT,
    version: 1,
    zone,
    cells: encodeCells(cells),
    saved_at: isoSeconds(now),
  };
}

// A never-saved week (null) is an empty week, not an error.
export async function loadWeek(api) {
  const body = await api.getUsual();
  if (body === null) return { cells: new Set(), zone: null };
  checkDoc(body, FORMAT);
  return { cells: decodeCells(body.cells), zone: body.zone || null };
}

const SAVE_FAILED = "Could not save. Check your connection and press Save again.";
const SAVE_MESSAGES = {
  too_large: "That week is too big to save.",
  rate_limited: "Too many saves just now. Wait a minute and press Save again.",
  not_enabled: NOT_ENABLED,
  bad_input: "That week couldn't be saved. Reload the page and try again.",
};
export const saveFailedMessage = (e) => (e && Object.hasOwn(SAVE_MESSAGES, e.code) ? SAVE_MESSAGES[e.code] : SAVE_FAILED);

const AUTOSAVE_MS = 1200;
const RETRY_MS = 15000;
const AUTOSAVE_FAILED = "Couldn't save just now. Trying again shortly.";

// A failed save worth trying again: a dropped connection (no code), a failing server, or the rate limit. Anything
// else (a refused week, an expired link) fails the same way every time.
const retryable = (e) => !(e instanceof LinkExpired) && (!e?.code || e.code === "rate_limited" || e.code.startsWith("http_5"));

// Saving as the player paints. touch() after each change restarts the delay, so a run of strokes is one save; one
// save runs at a time and a change made during it is saved after it. A failed save is tried again after RETRY_MS
// when retrying can help, else it says why and waits for the next change. flush() saves anything pending now and
// resolves once nothing is pending (or a save failed); cancel() drops it. save() does the PUT and throws on failure;
// status(text) reports.
export function autosaver(save, status, delay = AUTOSAVE_MS) {
  let timer = null;
  let dirty = false;
  let running = Promise.resolve();
  const later = (ms) => { clearTimeout(timer); timer = setTimeout(flush, ms); };
  function flush() {
    clearTimeout(timer);
    timer = null;
    running = running.then(async () => {
      if (!dirty) return;
      while (dirty) {
        dirty = false;
        status("Saving…");
        try {
          await save();
        } catch (e) {
          if (e instanceof LinkExpired) status(expiredMessage());
          else if (retryable(e)) { dirty = true; status(AUTOSAVE_FAILED); later(RETRY_MS); }
          else status(saveFailedMessage(e));
          return;
        }
      }
      status("Saved.");
    });
    return running;
  }
  return {
    touch: () => { dirty = true; later(delay); },
    flush,
    cancel: () => { clearTimeout(timer); timer = null; dirty = false; },
  };
}

const cellLabel = (labels, i) => {
  const d = Math.floor(i / PER_DAY), h = i % PER_DAY;
  const hh = Math.floor(h / 2), mm = h % 2 ? "30" : "00";
  return `${labels[d]} ${hourLabel(hh).replace(" ", ":" + mm + " ")}`;
};

// A paint grid's layout: columns, each { label, start, cells: [{ key, label }] }, whose cells run down
// from row start (half-hours after local midnight). The key is what the editor stores. weekColumns is the usual
// Mon-Sun week (key day * 48 + half-hour); windowColumns (grid.js) is a matchup's window, keyed by window cell.
export const weekColumns = (labels = DAYS) => labels.map((label, d) => ({
  label, start: 0, cells: Array.from({ length: PER_DAY }, (_, h) => ({ key: d * PER_DAY + h, label: cellLabel(labels, d * PER_DAY + h) })),
}));

// Where each key sits: pos maps a key to [column, row]; keyAt(column, row) is the key there, or undefined.
export function gridGeometry(columns) {
  const pos = new Map();
  const at = new Map();
  columns.forEach((c, x) => c.cells.forEach((cell, i) => {
    pos.set(cell.key, [x, c.start + i]);
    at.set(`${x},${c.start + i}`, cell.key);
  }));
  return { pos, keyAt: (x, y) => at.get(`${x},${y}`) };
}
const WEEK_GEOMETRY = gridGeometry(weekColumns());

// The editor's bottom buttons: the back button appears only when the caller can take the user back.
export const actionButtons = (onBack, backLabel = "Back to teams") => ["Save", "Clear", ...(onBack ? [backLabel] : [])];

// A page's way into the editor and back. dispose stops the page's timers and listeners before the editor takes
// over; show(doc, status) draws the page again, with the saved week and "Saved." or with the old week on Back.
export function weekEditing(root, api, { now, backLabel, dispose, show }) {
  return (doc) => {
    dispose();
    root.classList.remove("wide");
    return mountUsual(root, api, { now, backLabel, onSaved: (saved) => show(saved, "Saved."), onBack: () => show(doc, "") });
  };
}

// The paintable grid both week editors draw, in gridFrame's frame, laid out by columns (see weekColumns).
// onChange runs after every stroke or key toggle. It opens on the first day's tab with the given cells (keys) painted.
export function paintGrid({ columns, gridLabel, cells: initial, onChange }) {
  let cells = initial;
  const geo = gridGeometry(columns);
  const nodes = new Map();
  const { tabs, grid, setDay } = gridFrame(columns, {
    class: "grid", role: "grid", "aria-label": gridLabel, "aria-multiselectable": "true",
  }, ({ key, label }, row) => {
    const b = el("button", { type: "button", class: `cell ${row}`, "data-i": String(key), role: "gridcell", tabindex: "-1", "aria-label": label });
    nodes.set(key, b);
    return b;
  });
  nodes.values().next().value.tabIndex = 0;

  const paintView = (set, preview) => {
    for (const [k, n] of nodes) {
      const on = set.has(k);
      n.classList.toggle("on", on);
      n.setAttribute("aria-selected", String(on));
    }
    if (preview) for (const k of preview) nodes.get(k).classList.add("stroke");
    else nodes.forEach((n) => n.classList.remove("stroke"));
  };

  const cellAt = (e) => {
    const t = document.elementFromPoint(e.clientX, e.clientY);
    const c = t && t.closest && t.closest(".cell");
    return c ? Number(c.dataset.i) : null;
  };
  const stroke = (base, from, to, paint) => applyStroke(base, from, to, paint, geo);

  let drag = null; // { anchor, paint, base, current }
  const endStroke = (commit) => {
    if (!drag) return;
    if (commit) cells = stroke(drag.base, drag.anchor, drag.current, drag.paint);
    drag = null;
    paintView(cells);
    onChange();
  };
  grid.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    const i = cellAt(e);
    if (i === null) return;
    drag = { anchor: i, current: i, paint: !cells.has(i), base: new Set(cells) };
    if (e.pointerType === "mouse") e.preventDefault();
    grid.setPointerCapture?.(e.pointerId);
    paintView(cells, stroke(new Set(), i, i, true));
  });
  grid.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const i = cellAt(e);
    if (i === null || i === drag.current) return;
    drag.current = i;
    paintView(stroke(drag.base, drag.anchor, i, drag.paint), stroke(new Set(), drag.anchor, i, true));
  });
  grid.addEventListener("pointerup", () => endStroke(true));
  grid.addEventListener("pointercancel", () => endStroke(false));

  // Keyboard: arrows move to the cell beside, Space/Enter toggles the focused cell.
  grid.addEventListener("keydown", (e) => {
    const c = e.target.closest?.(".cell");
    if (!c) return;
    const i = Number(c.dataset.i);
    const step = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }[e.key];
    if (step !== undefined) {
      e.preventDefault();
      const [x, y] = geo.pos.get(i);
      const j = geo.keyAt(x + step[0], y + step[1]);
      if (j === undefined) return;
      setDay(x + step[0]);
      c.tabIndex = -1; nodes.get(j).tabIndex = 0; nodes.get(j).focus();
    } else if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      cells = stroke(cells, i, i, !cells.has(i));
      paintView(cells);
      onChange();
    }
  });

  paintView(cells);
  return {
    tabs, grid,
    cells: () => cells,
    setCells: (next) => { cells = next; paintView(cells); },
  };
}

// Every zone the browser knows, else just fallback on a browser without Intl.supportedValuesOf.
export const allZones = (fallback) => (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [fallback]);

// The time zone picker both editors and the heatmap pages show: zones (else every zone the browser knows, else
// fallback), with zone first if missing, and zone selected. row is the labelled picker to place on the page; id and
// label let a page hold two of them.
export function zoneSelect(zone, zones, fallback = zone, { id = "zone", label = "Time zone" } = {}) {
  const list = zones || allZones(fallback);
  const select = el("select", { id, "aria-label": label });
  for (const z of list.includes(zone) ? list : [zone, ...list]) select.append(el("option", { value: z, text: zoneName(z) }));
  select.value = zone;
  return { select, row: el("label", { class: "zonerow", for: id }, `${label} `, select) };
}

// An editor's lede. It opens with the rule people most often miss: paint the whole time you're free, not just the
// half-hour you'd like to start, since every grid counts you free only on the half-hours you painted.
export const paintLede = (text) => el("p", { class: "lede" },
  el("strong", { text: "Paint every half-hour you're free to play, not just when you'd like to start." }), ` ${text}`);

// The hint shown above a paint grid on a touch screen.
export const touchHint = () => el("p", { class: "touchhint", text: "Drag on the grid to paint. Drag the times on the left to scroll." });

// deps: { now?: () => Date, zones?: string[], defaultZone?: string, intro?: string, onSaved?: (doc) => void,
//         onBack?: () => void, backLabel?: string, startUtc?: Date, saveDelay?: number }
// With onBack the editor also offers a back button (backLabel, else Back to teams), leaving without saving.
// With startUtc it sits on a matchup page under the matchup's heading: no app bar or heading of its own, the grid
// laid out as that matchup's window (windowColumns) instead of Mon-Sun, and no Save button, since it saves as the
// player paints (saveDelay ms after the last change). Returns { flush } (see autosaver).
// With onSaved the editor is a step in another flow: a save says "Saved." and hands the saved file on.
export async function mountUsual(root, api, deps = {}) {
  const onSaved = deps.onSaved;
  const now = deps.now || (() => new Date());
  const browserZone = deps.defaultZone || Intl.DateTimeFormat().resolvedOptions().timeZone;

  root.replaceChildren(el("p", { class: "status", text: "Loading your week…" }));
  let loaded;
  try {
    loaded = await loadWeek(api);
  } catch (e) {
    if (e instanceof LinkExpired) { root.replaceChildren(el("p", { class: "notice", text: expiredMessage() })); return; }
    throw e;
  }

  let zone = loaded.zone || browserZone;
  const { select, row: zoneRow } = zoneSelect(zone, deps.zones, browserZone);

  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { type: "button", class: "primary", text: "Save" });
  const clear = el("button", { type: "button", class: "secondary", text: "Clear" });
  const backLabel = actionButtons(deps.onBack, deps.backLabel)[2];
  const back = backLabel ? el("button", { type: "button", class: "secondary", text: backLabel }) : null;
  if (back) back.addEventListener("click", () => deps.onBack());
  // On a matchup the grid paints window cells as instants; week is the usual week behind them, which keeps any cell
  // the window never reaches. Off a matchup the grid's keys are the usual week's own cells.
  const startUtc = deps.startUtc;
  let week = loaded.cells;
  const weekOf = () => (startUtc ? usualWithWindow(week, painter.cells(), zone, startUtc) : painter.cells());
  const write = async () => {
    week = weekOf();
    const doc = toFile(zone, week, now());
    await api.putUsual(doc);
    if (onSaved) onSaved(doc);
  };
  const auto = startUtc ? autosaver(write, (text) => { status.textContent = text; }, deps.saveDelay) : null;
  const touched = () => { status.textContent = ""; auto?.touch(); };

  let painter;
  const paintBox = el("div");
  const draw = () => {
    painter = paintGrid({
      columns: startUtc ? windowColumns(startUtc, zone) : weekColumns(), gridLabel: "Usual week, half-hour cells",
      cells: startUtc ? windowCells(zone, week, startUtc) : week, onChange: touched,
    });
    paintBox.replaceChildren(painter.tabs, touchHint(), painter.grid);
  };
  draw();

  // A new zone keeps the painted wall times; on a matchup that moves them within the window, so it is drawn again.
  select.addEventListener("change", () => {
    week = weekOf();
    zone = select.value;
    if (startUtc) draw();
    touched();
  });
  clear.addEventListener("click", () => { week = new Set(); painter.setCells(new Set()); touched(); });
  save.addEventListener("click", async () => {
    if (api.fixture && !onSaved) { status.textContent = "Saved (fixture mode: nothing was sent)."; return; }
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      await write();
      status.textContent = onSaved ? "Saved." : "Saved. It shows in /settings straight away.";
    } catch (e) {
      status.textContent = e instanceof LinkExpired ? expiredMessage() : saveFailedMessage(e);
    } finally {
      save.disabled = false;
    }
  });

  root.replaceChildren(
    ...(startUtc ? [] : [appBar("Your usual week")]),
    ...(deps.intro ? [el("p", { class: "notice intro", text: deps.intro })] : []),
    ...(startUtc ? [] : [el("h1", { text: "When are you usually free?" })]),
    paintLede("A draft and its matches take a few hours. Drag or tap to paint; drag again over painted cells to erase. Times are local to the zone below."),
    zoneRow,
    paintBox,
    el("div", { class: "actions" }, ...(auto ? [] : [save]), clear, ...(back ? [back] : [])),
    status,
  );
  return { flush: () => auto?.flush() ?? Promise.resolve() };
}
