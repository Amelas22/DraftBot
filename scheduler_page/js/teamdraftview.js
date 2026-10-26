// Team draft, the view: the two sides, one heatmap of when they are free together with the good start times
// marked, and Book / Move / Clear for the matchup's time. Every decision lives in teamdraft.js; this file only
// draws and wires.
import { LinkExpired, checkDoc } from "./api.js";
import { CELLS, CELL_MS } from "./week.js";
import { mountHeatmap, formatCell } from "./grid.js";
import { appBar } from "./appbar.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { plural } from "./words.js";
import { actionErrorText, createSync, shadeLevel } from "./teams.js";
import { FORMAT, bookable, bookingLine, freeBySide, goodStarts, refusalText, shiftSelection } from "./teamdraft.js";

const PATH = "/api/team-draft";
const ACTIONS_PATH = "/api/team-draft/actions";
const REFRESH_MS = 30000;
const HOVER_HINT = "Hover or tap a half-hour to see who's free. Tap a time to pick it.";
const NO_WEEK = "no usual week set";

// deps: { now?: () => Date }. Returns { dispose } which stops the refresh timer.
export async function mountTeamDraft(root, api, deps = {}) {
  const now = deps.now || (() => new Date());
  const cleanups = [];
  const controller = { dispose: () => { while (cleanups.length) cleanups.pop()(); } };
  const notice = (text) => { controller.dispose(); root.classList.remove("wide"); root.replaceChildren(el("p", { class: "notice", text })); };

  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  let zone;
  let state;
  try {
    // Times are shown in the viewer's usual-week zone, else the browser's.
    zone = (await api.getUsual())?.zone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    state = checkDoc(await api.get(PATH), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) { notice(expiredMessage()); return controller; }
    throw e;
  }

  let startUtc = new Date(state.window.start_utc);
  const sync = createSync();
  let freeSides = [];
  let good = new Set();
  let selected = null;

  const heading = el("h1");
  const ledeEl = el("p", { class: "lede" });
  const bookLine = el("p", { class: "bookline", role: "status", "aria-live": "polite" });
  const threadLink = el("a", { class: "threadlink", text: "Open the thread", target: "_blank", rel: "noopener" });
  const sidesBox = el("div", { class: "sides" });
  const heatBox = el("div", { class: "heatbox" });
  const legend = el("div", { class: "legend" });
  const readout = el("p", { class: "readout", "aria-live": "polite", text: HOVER_HINT });
  const bookBtn = el("button", { type: "button", class: "primary", text: "Book this time" });
  const moveBtn = el("button", { type: "button", class: "primary", text: "Move here" });
  const clearBtn = el("button", { type: "button", class: "secondary", text: "Clear" });
  const actions = el("div", { class: "actions" }, bookBtn, moveBtn, clearBtn);
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const pane = el("section", { class: "pane", "aria-label": "Pick a time" }, heatBox, legend, readout, actions, status);

  const open = () => state.matchup.state === "open";
  const people = () => state.sides.flatMap((s) => s.people);
  const total = () => people().length;
  const freeCount = (j) => freeSides.reduce((n, side) => n + side.reduce((m, free) => m + (free.has(j) ? 1 : 0), 0), 0);
  const bookedCell = () => {
    if (!state.booking.start_utc) return null;
    const j = (Date.parse(state.booking.start_utc) - startUtc.getTime()) / CELL_MS;
    return Number.isInteger(j) && j >= 0 && j < CELLS ? j : null;
  };
  const isBooked = () => state.booking.start_utc !== null;
  const draftCells = () => state.rules.draft_cells;

  const label = (j) => {
    const n = freeCount(j);
    const notes = [`${n} of ${total()} free`];
    if (good.has(j)) notes.push("good start");
    if (j === bookedCell()) notes.push("booked");
    return `${formatCell(startUtc, zone, j)}: ${notes.join(", ")}`;
  };
  const cellClass = (j) => {
    const booked = bookedCell();
    const classes = [`s${shadeLevel(freeCount(j), Math.max(1, total()))}`];
    if (good.has(j)) classes.push("good");
    if (booked !== null && j >= booked && j < booked + draftCells()) classes.push("booked");
    if (j === selected) classes.push("picked");
    return classes.join(" ");
  };
  const makeHeat = () => mountHeatmap(heatBox, {
    startUtc, zone, cellClass, cellLabel: label,
    onPick: (j) => {
      selected = open() && bookable(j, state, now(), startUtc) ? j : null;
      readout.textContent = label(j);
      renderActions();
      heat.update();
    },
    onHover: (j) => { readout.textContent = j === null ? HOVER_HINT : label(j); },
  });
  let heat = makeHeat();

  const renderSides = () => {
    sidesBox.replaceChildren(...state.sides.map((side) => el("section", { class: "side", "aria-label": side.label },
      el("h2", { text: side.label }),
      el("ul", {}, ...side.people.map((p) => {
        const li = el("li", { text: p.name });
        if (!p.cells || !p.zone) li.append(el("span", { class: "noweek", text: ` (${NO_WEEK})` }));
        return li;
      })))));
  };

  const renderLegend = () => {
    const swatches = [];
    for (let n = 0; n <= total(); n++) swatches.push(el("i", { class: `s${shadeLevel(n, Math.max(1, total()))}` }));
    legend.replaceChildren(el("span", { text: "nobody" }), ...swatches, el("span", { text: `all ${total()} free` }),
      el("i", { class: "key good" }), el("span", { text: "good start" }), el("i", { class: "key booked" }), el("span", { text: "booked" }));
  };

  function renderActions() {
    const canPick = open();
    const picked = canPick && selected !== null && bookable(selected, state, now(), startUtc);
    if (!picked) selected = null;
    bookBtn.hidden = !(picked && !isBooked());
    moveBtn.hidden = !(picked && isBooked() && selected !== bookedCell());
    clearBtn.hidden = !(canPick && isBooked());
    actions.hidden = bookBtn.hidden && moveBtn.hidden && clearBtn.hidden;
  }

  function render() {
    heading.textContent = state.matchup.name;
    bookLine.textContent = bookingLine(state, zone);
    threadLink.hidden = !state.matchup.thread_url;
    if (state.matchup.thread_url) threadLink.href = state.matchup.thread_url;
    freeSides = freeBySide(state, startUtc);
    good = goodStarts(state, startUtc);
    ledeEl.textContent = ledeText();
    renderSides();
    renderLegend();
    renderActions();
    heat.update();
  }

  const ledeText = () => {
    const where = `Times in ${zone.replaceAll("_", " ")}.`;
    if (!open()) return `This draft has started. ${where}`;
    return `${plural(good.size, "good start")} in the next 7 days, outlined on the grid. ${where}`;
  };

  // A finished action (or a failed one) unlocks refreshes again.
  const send = async (body) => {
    const done = sync.startAction();
    for (const b of [bookBtn, moveBtn, clearBtn]) b.disabled = true;
    status.textContent = "Sending…";
    try {
      const r = await api.post(ACTIONS_PATH, { action_id: crypto.randomUUID(), seen_version: state.booking.version, ...body });
      state = checkDoc(r?.state, FORMAT);
      selected = null;
      render();
      status.textContent = r.ok ? "" : refusalText(r.code);
    } catch (e) {
      status.textContent = actionErrorText(e);
      if (e instanceof LinkExpired) cleanups.forEach((c) => c());
    } finally {
      done();
      for (const b of [bookBtn, moveBtn, clearBtn]) b.disabled = false;
    }
  };

  const startOf = (j) => new Date(startUtc.getTime() + j * CELL_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  bookBtn.addEventListener("click", () => { if (selected !== null) send({ kind: "book", start_utc: startOf(selected) }); });
  moveBtn.addEventListener("click", () => { if (selected !== null) send({ kind: "book", start_utc: startOf(selected) }); });
  clearBtn.addEventListener("click", () => send({ kind: "clear" }));

  async function refresh() {
    const ticket = sync.startRefresh();
    if (!ticket) return;
    try {
      const fresh = checkDoc(await api.get(PATH), FORMAT);
      if (!sync.isCurrent(ticket)) return;
      state = fresh;
      const start = new Date(state.window.start_utc);
      if (start.getTime() !== startUtc.getTime()) {
        // The window slid forward: keep the picked moment, not its cell index.
        const had = selected !== null;
        selected = shiftSelection(selected, startUtc, start);
        if (had && selected === null) readout.textContent = HOVER_HINT;
        startUtc = start;
        heat = makeHeat();
      }
      render();
    } catch (e) {
      if (e instanceof LinkExpired) notice(expiredMessage());
      else console.warn("team draft refresh failed", e);
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, REFRESH_MS);
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  cleanups.push(() => clearInterval(timer), () => document.removeEventListener("visibilitychange", onVisible));

  root.classList.add("wide");
  root.replaceChildren(appBar("Team draft"), heading, ledeEl, bookLine, threadLink, sidesBox, pane);
  render();
  return controller;
}
