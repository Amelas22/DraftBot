// The usual-week editor: a pure model (applyStroke, toFile, loadWeek) plus a thin DOM view (mountUsual).
import { CELLS, decodeCells, encodeCells } from "./week.js";
import { NotFound, LinkExpired } from "./api.js";
import { expiredMessage, brokenMessage } from "./messages.js";
import { el } from "./dom.js";

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
    saved_at: now.toISOString().replace(/\.\d+Z$/, "Z"),
  };
}

// A never-saved week (404) is an empty week, not an error.
export async function loadWeek(url, getJson) {
  let body;
  try {
    body = await getJson(url);
  } catch (e) {
    if (e instanceof NotFound) return { cells: new Set(), zone: null };
    throw e;
  }
  if (!body || body.format !== FORMAT) throw new Error("unknown usual-week format");
  if (!Number.isInteger(body.version) || body.version > 1) throw new Error("usual week is newer than this page");
  return { cells: decodeCells(body.cells), zone: body.zone || null };
}

const hourLabel = (h) => {
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12} ${h < 12 ? "AM" : "PM"}`;
};
const cellLabel = (i) => {
  const d = Math.floor(i / PER_DAY), h = i % PER_DAY;
  const hh = Math.floor(h / 2), mm = h % 2 ? "30" : "00";
  return `${DAYS[d]} ${hourLabel(hh).replace(" ", ":" + mm + " ")}`;
};

// deps: { getJson, putJson, now?: () => Date, zones?: string[], defaultZone?: string }
export async function mountUsual(root, link, deps) {
  const now = deps.now || (() => new Date());
  const browserZone = deps.defaultZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zoneList = deps.zones || (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [browserZone]);

  root.replaceChildren(el("p", { class: "status", text: "Loading your week…" }));
  let loaded;
  try {
    loaded = await loadWeek(link.urls.usual_get, deps.getJson);
  } catch (e) {
    if (e instanceof LinkExpired) { root.replaceChildren(el("p", { class: "notice", text: expiredMessage(link.view) })); return; }
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
    if (link.fixture) { status.textContent = "Saved (fixture mode: nothing was sent)."; return; }
    if (!link.urls.usual_put) { status.textContent = brokenMessage(link.view); return; }
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      await deps.putJson(link.urls.usual_put, toFile(zone, cells, now()));
      status.textContent = "Saved. Discord shows it within a minute.";
    } catch (e) {
      status.textContent = e instanceof LinkExpired ? expiredMessage(link.view) : "Could not save. Check your connection and press Save again.";
    } finally {
      save.disabled = false;
    }
  });

  root.replaceChildren(
    el("p", { class: "eyebrow", text: "Your usual week" }),
    el("h1", { text: "When are you usually free?" }),
    el("p", { class: "lede", text: "Drag or tap to paint the half-hours you can usually draft. Times are local to the zone below. Drag again over painted cells to erase." }),
    el("label", { class: "zonerow", for: "zone" }, "Time zone ", select),
    tabs, el("p", { class: "touchhint", text: "Drag on the grid to paint. Drag the times on the left to scroll." }), grid,
    el("div", { class: "actions" }, save, clear),
    status,
  );
  setDay(0);
  paintView(cells);
}
