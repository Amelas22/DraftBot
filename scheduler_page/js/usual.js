// The usual-week editor: a pure model (applyStroke, toFile, loadWeek) plus a thin DOM view (mountUsual).
import { CELLS, decodeCells, encodeCells, isoSeconds } from "./week.js";
import { LinkExpired, checkDoc } from "./api.js";
import { expiredMessage, NOT_ENABLED } from "./messages.js";
import { el } from "./dom.js";
import { appBar } from "./appbar.js";
import { hourLabel } from "./grid.js";

const FORMAT = "draftbot.scheduler.usual_week";
const PER_DAY = 48;
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Fill the rectangle between two grid positions (same days range x same half-hour range).
export function applyStroke(cells, from, to, paint) {
  const out = new Set(cells);
  const d0 = Math.min(Math.floor(from / PER_DAY), Math.floor(to / PER_DAY));
  const d1 = Math.max(Math.floor(from / PER_DAY), Math.floor(to / PER_DAY));
  const h0 = Math.min(from % PER_DAY, to % PER_DAY);
  const h1 = Math.max(from % PER_DAY, to % PER_DAY);
  for (let d = d0; d <= d1; d++) {
    for (let h = h0; h <= h1; h++) {
      if (paint) out.add(d * PER_DAY + h);
      else out.delete(d * PER_DAY + h);
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
};
export const saveFailedMessage = (e) => (e && Object.hasOwn(SAVE_MESSAGES, e.code) ? SAVE_MESSAGES[e.code] : SAVE_FAILED);

const cellLabel = (i) => {
  const d = Math.floor(i / PER_DAY), h = i % PER_DAY;
  const hh = Math.floor(h / 2), mm = h % 2 ? "30" : "00";
  return `${DAYS[d]} ${hourLabel(hh).replace(" ", ":" + mm + " ")}`;
};

// The editor's bottom buttons: the back button appears only when the caller can take the user back.
export const actionButtons = (onBack, backLabel = "Back to teams") => ["Save", "Clear", ...(onBack ? [backLabel] : [])];

// The button another page offers for opening the editor: null is a week never saved.
export const editWeekLabel = (usualDoc) => (usualDoc === null ? "Set my usual week" : "Edit my usual week");

// A page's way into the editor and back. dispose stops the page's timers and listeners before the editor takes
// over; show(doc, status) draws the page again, with the saved week and "Saved." or with the old week on Back.
export function weekEditing(root, api, { now, backLabel, dispose, show }) {
  return (doc) => {
    dispose();
    root.classList.remove("wide");
    return mountUsual(root, api, { now, backLabel, onSaved: (saved) => show(saved, "Saved."), onBack: () => show(doc, "") });
  };
}

// deps: { now?: () => Date, zones?: string[], defaultZone?: string, intro?: string, onSaved?: (doc) => void,
//         onBack?: () => void, backLabel?: string }
// With onBack the editor also offers a back button (backLabel, else Back to teams), leaving without saving.
// With onSaved the editor is a step in another flow: a save says "Saved." and hands the saved file on.
export async function mountUsual(root, api, deps = {}) {
  const onSaved = deps.onSaved;
  const now = deps.now || (() => new Date());
  const browserZone = deps.defaultZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zoneList = deps.zones || (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [browserZone]);

  root.replaceChildren(el("p", { class: "status", text: "Loading your week…" }));
  let loaded;
  try {
    loaded = await loadWeek(api);
  } catch (e) {
    if (e instanceof LinkExpired) { root.replaceChildren(el("p", { class: "notice", text: expiredMessage() })); return; }
    throw e;
  }

  let cells = loaded.cells;
  let zone = loaded.zone || browserZone;
  const zones = zoneList.includes(zone) ? zoneList : [zone, ...zoneList];

  const select = el("select", { id: "zone", "aria-label": "Time zone" });
  for (const z of zones) select.append(el("option", { value: z, text: z.replaceAll("_", " ") }));
  select.value = zone;

  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { type: "button", class: "primary", text: "Save" });
  const clear = el("button", { type: "button", class: "secondary", text: "Clear" });
  const backLabel = actionButtons(deps.onBack, deps.backLabel)[2];
  const back = backLabel ? el("button", { type: "button", class: "secondary", text: backLabel }) : null;
  if (back) back.addEventListener("click", () => deps.onBack());

  // Day tabs: only visible under 480px (CSS), where one day fills the screen.
  const tabs = el("div", { class: "daytabs", role: "tablist", "aria-label": "Day" });
  const tabButtons = DAYS.map((d, i) => {
    const b = el("button", { type: "button", role: "tab", text: d });
    b.addEventListener("click", () => setDay(i));
    tabs.append(b);
    return b;
  });
  const setDay = (day) => {
    grid.dataset.active = String(day);
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(i === day)));
  };

  const grid = el("div", { class: "grid", role: "grid", "aria-label": "Usual week, half-hour cells", "aria-multiselectable": "true" });
  const hours = el("div", { class: "col hours", "aria-hidden": "true" });
  for (let h = 0; h < PER_DAY; h++) hours.append(el("div", { class: "hour", text: h % 2 === 0 ? hourLabel(h / 2) : "" }));
  grid.append(hours);

  const nodes = [];
  for (let d = 0; d < 7; d++) {
    const col = el("div", { class: "col day", "data-day": String(d), role: "row" });
    col.append(el("div", { class: "dayname", text: DAYS[d], "aria-hidden": "true" }));
    for (let h = 0; h < PER_DAY; h++) {
      const i = d * PER_DAY + h;
      const b = el("button", { type: "button", class: "cell", "data-i": String(i), role: "gridcell", tabindex: "-1", "aria-label": cellLabel(i) });
      nodes[i] = b;
      col.append(b);
    }
    grid.append(col);
  }
  nodes[0].tabIndex = 0;

  const paintView = (set, preview) => {
    for (let i = 0; i < CELLS; i++) {
      const on = set.has(i);
      nodes[i].classList.toggle("on", on);
      nodes[i].setAttribute("aria-selected", String(on));
    }
    if (preview) for (const i of preview) nodes[i].classList.add("stroke");
    else nodes.forEach((n) => n.classList.remove("stroke"));
  };

  const cellAt = (e) => {
    const t = document.elementFromPoint(e.clientX, e.clientY);
    const c = t && t.closest && t.closest(".cell");
    return c ? Number(c.dataset.i) : null;
  };

  let stroke = null; // { anchor, paint, base, current }
  const touched = () => { status.textContent = ""; };
  const endStroke = (commit) => {
    if (!stroke) return;
    if (commit) cells = applyStroke(stroke.base, stroke.anchor, stroke.current, stroke.paint);
    stroke = null;
    paintView(cells);
    touched();
  };
  grid.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    const i = cellAt(e);
    if (i === null) return;
    stroke = { anchor: i, current: i, paint: !cells.has(i), base: new Set(cells) };
    if (e.pointerType === "mouse") e.preventDefault();
    grid.setPointerCapture?.(e.pointerId);
    paintView(cells, applyStroke(new Set(), i, i, true));
  });
  grid.addEventListener("pointermove", (e) => {
    if (!stroke) return;
    const i = cellAt(e);
    if (i === null || i === stroke.current) return;
    stroke.current = i;
    paintView(applyStroke(stroke.base, stroke.anchor, i, stroke.paint), applyStroke(new Set(), stroke.anchor, i, true));
  });
  grid.addEventListener("pointerup", () => endStroke(true));
  grid.addEventListener("pointercancel", () => endStroke(false));

  // Keyboard: arrows move, Space/Enter toggles the focused cell.
  grid.addEventListener("keydown", (e) => {
    const c = e.target.closest?.(".cell");
    if (!c) return;
    const i = Number(c.dataset.i);
    const step = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -PER_DAY, ArrowRight: PER_DAY }[e.key];
    if (step !== undefined) {
      e.preventDefault();
      const j = i + step;
      if (j < 0 || j >= CELLS || (Math.abs(step) === 1 && Math.floor(j / PER_DAY) !== Math.floor(i / PER_DAY))) return;
      setDay(Math.floor(j / PER_DAY));
      c.tabIndex = -1; nodes[j].tabIndex = 0; nodes[j].focus();
    } else if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      cells = applyStroke(cells, i, i, !cells.has(i));
      paintView(cells);
      touched();
    }
  });

  select.addEventListener("change", () => { zone = select.value; touched(); });
  clear.addEventListener("click", () => { cells = new Set(); paintView(cells); touched(); });
  save.addEventListener("click", async () => {
    if (api.fixture && !onSaved) { status.textContent = "Saved (fixture mode: nothing was sent)."; return; }
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      const doc = toFile(zone, cells, now());
      await api.putUsual(doc);
      status.textContent = onSaved ? "Saved." : "Saved. It shows in /settings straight away.";
      if (onSaved) onSaved(doc);
    } catch (e) {
      status.textContent = e instanceof LinkExpired ? expiredMessage() : saveFailedMessage(e);
    } finally {
      save.disabled = false;
    }
  });

  root.replaceChildren(
    appBar("Your usual week"),
    ...(deps.intro ? [el("p", { class: "notice intro", text: deps.intro })] : []),
    el("h1", { text: "When are you usually free?" }),
    el("p", { class: "lede", text: "Drag or tap to paint the half-hours you can usually draft. Times are local to the zone below. Drag again over painted cells to erase." }),
    el("label", { class: "zonerow", for: "zone" }, "Time zone ", select),
    tabs, el("p", { class: "touchhint", text: "Drag on the grid to paint. Drag the times on the left to scroll." }), grid,
    el("div", { class: "actions" }, save, clear, ...(back ? [back] : [])),
    status,
  );
  setDay(0);
  paintView(cells);
}
