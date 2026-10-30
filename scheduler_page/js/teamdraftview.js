// Team draft, the view: the two sides, one heatmap of when they are free together with each run of the same people
// boxed and its counts written once, Book / Move / Clear for the matchup's time, and a way to paint or edit the viewer's own usual week. Pressing a
// side or a player leaves them out of the grid until pressed again. Every decision lives in teamdraft.js; this file
// only draws and wires.
import { LinkExpired, checkDoc } from "./api.js";
import { CELLS, CELL_MS, addCells, isoSeconds } from "./week.js";
import { mountHeatmap, formatCell, layoutWindow } from "./grid.js";
import { appBar } from "./appbar.js";
import { editWeekLabel, weekEditing } from "./usual.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { actionErrorText, createSync, shadeLevel } from "./teams.js";
import {
  FORMAT, bookable, bookingLine, countText, freeBySide, freeRuns, refusalText, shiftSelection, sideCounts, toggleSide,
  togglePerson, withoutPeople,
} from "./teamdraft.js";

const TEAM_DRAFT_PATHS = { get: "/api/team-draft", actions: "/api/team-draft/actions" };
const REFRESH_MS = 30000;
const HOVER_HINT = "Hover or tap a half-hour to see who's free on each side. Tap a time to pick it.";
const TOGGLE_HINT = "Tap a team or a player to leave them out of the grid; tap again to bring them back.";
const NO_WEEK = "no usual week set";
const BACK = "Back to the matchup";

// deps: { now?: () => Date, paths?: { get, actions } } (paths default to the team-draft API; a match link passes its own). Returns { dispose } which stops the refresh timer.
export async function mountTeamDraft(root, api, deps = {}) {
  const now = deps.now || (() => new Date());
  const paths = deps.paths || TEAM_DRAFT_PATHS;
  const cleanups = [];
  const controller = { dispose: () => { while (cleanups.length) cleanups.pop()(); } };
  const notice = (text) => { controller.dispose(); root.classList.remove("wide"); root.replaceChildren(el("p", { class: "notice", text })); };

  // The matchup's refresh loop and listeners are disposed before the editor takes the page; show registers them
  // afresh when the matchup comes back, refetched so the viewer's new week is in it.
  const show = (usualDoc, firstStatus) => showMatchup(root, api, paths, usualDoc, now, cleanups, notice, firstStatus, editWeek)
    .catch((e) => {
      if (e instanceof LinkExpired) notice(expiredMessage());
      else { console.error(e); notice(actionErrorText(e)); }
    });
  const editWeek = weekEditing(root, api, { now, backLabel: BACK, dispose: controller.dispose, show });

  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  let usual;
  try {
    usual = await api.getUsual();
  } catch (e) {
    if (e instanceof LinkExpired) { notice(expiredMessage()); return controller; }
    throw e;
  }
  await showMatchup(root, api, paths, usual, now, cleanups, notice, "", editWeek);
  return controller;
}

async function showMatchup(root, api, paths, usualDoc, now, cleanups, notice, firstStatus, editWeek) {
  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  let state;
  try {
    state = checkDoc(await api.get(paths.get), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) return notice(expiredMessage());
    throw e;
  }
  // Times are shown in the viewer's usual-week zone, else the browser's.
  const zone = usualDoc?.zone || Intl.DateTimeFormat().resolvedOptions().timeZone;

  let startUtc = new Date(state.window.start_utc);
  const sync = createSync();
  let freeSides = [];
  let runs = [];
  let selected = null;
  // User ids the viewer has switched off; kept across refreshes.
  let off = new Set();

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
  const status = el("p", { class: "status", role: "status", "aria-live": "polite", text: firstStatus });
  const pane = el("section", { class: "pane", "aria-label": "Pick a time" }, heatBox, legend, readout, actions, status);

  const open = () => state.matchup.state === "open";
  const shown = () => withoutPeople(state, off);
  const everyone = () => state.sides.flatMap((s) => s.people);
  const total = () => shown().sides.reduce((n, s) => n + s.people.length, 0);
  const freeCount = (j) => sideCounts(freeSides, j).reduce((a, b) => a + b, 0);
  const bookedCell = () => {
    if (!state.booking.start_utc) return null;
    const j = (Date.parse(state.booking.start_utc) - startUtc.getTime()) / CELL_MS;
    return Number.isInteger(j) && j >= 0 && j < CELLS ? j : null;
  };
  const isBooked = () => state.booking.start_utc !== null;
  const draftCells = () => state.rules.draft_cells;

  const label = (j) => {
    const notes = [`${countText(sideCounts(freeSides, j))} free`];
    if (j === bookedCell()) notes.push("booked");
    return `${formatCell(startUtc, zone, j)}: ${notes.join(", ")}`;
  };
  const cellClass = (j) => {
    const booked = bookedCell();
    const classes = [`s${shadeLevel(freeCount(j), Math.max(1, total()))}`];
    const run = runs[j];
    if (run) classes.push("run", ...(run.first ? ["rfirst"] : []), ...(run.last ? ["rlast"] : []));
    if (booked !== null && j >= booked && j < booked + draftCells()) classes.push("booked");
    if (j === selected) classes.push("picked");
    return classes.join(" ");
  };
  const makeHeat = () => mountHeatmap(heatBox, {
    startUtc, zone, cellClass, cellLabel: label, cellText: (j) => (runs[j]?.first ? countText(sideCounts(freeSides, j)) : ""),
    onPick: (j) => {
      selected = open() && bookable(j, state, now(), startUtc) ? j : null;
      readout.textContent = label(j);
      renderActions();
      heat.update();
    },
    onHover: (j) => { readout.textContent = j === null ? HOVER_HINT : label(j); },
  });
  let heat = makeHeat();

  const toggle = (next) => { off = next; render(); };
  // A toggle chip: the tick is decoration, so it stays out of the button's name; aria-pressed carries the state.
  const chip = (name, on) => el("button", { type: "button", class: `toggle${on ? "" : " off"}`, "aria-pressed": String(on) },
    el("span", { class: "tick", "aria-hidden": "true", text: "✓ " }), name);
  const renderSides = () => {
    sidesBox.replaceChildren(el("p", { class: "togglehint", text: TOGGLE_HINT }), ...state.sides.map((side) => {
      const sideOn = side.people.some((p) => !off.has(p.user));
      const head = chip(side.label, sideOn);
      head.addEventListener("click", () => toggle(toggleSide(off, side)));
      return el("section", { class: "side", "aria-label": side.label },
        el("h2", {}, head),
        el("ul", {}, ...side.people.map((p) => {
          const on = !off.has(p.user);
          const b = chip(p.name, on);
          b.addEventListener("click", () => toggle(togglePerson(off, p.user)));
          const li = el("li", {}, b);
          if (!p.cells || !p.zone) li.append(el("span", { class: "noweek", text: ` (${NO_WEEK})` }));
          return li;
        })));
    }));
  };

  // A key and its words, kept together when the legend wraps on a narrow screen.
  const keyItem = (kind, text) => el("span", { class: "keyitem" }, el("i", { class: `key ${kind}` }), el("span", { text }));
  const renderLegend = () => {
    const swatches = [];
    for (let n = 0; n <= total(); n++) swatches.push(el("i", { class: `s${shadeLevel(n, Math.max(1, total()))}` }));
    legend.replaceChildren(el("span", { text: "nobody" }), ...swatches, el("span", { text: `all ${total()} free` }),
      keyItem("run", "same people throughout"), keyItem("booked", "booked"));
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
    freeSides = freeBySide(shown(), startUtc);
    runs = freeRuns(freeSides, new Set(layoutWindow(startUtc, zone).map((d) => d.cells[0].j)));
    ledeEl.textContent = ledeText();
    renderSides();
    renderLegend();
    renderActions();
    heat.update();
  }

  const ledeText = () => {
    const where = `Times in ${zone.replaceAll("_", " ")}.`;
    const showing = total() < everyone().length ? ` Showing ${total()} of ${everyone().length} players.` : "";
    if (!open()) return `This draft has started. ${where}${showing}`;
    return `Each box is a stretch when the same people stay free, with how many on each side at its top. ${where}${showing}`;
  };

  // A finished action (or a failed one) unlocks refreshes again.
  const send = async (body) => {
    const done = sync.startAction();
    for (const b of [bookBtn, moveBtn, clearBtn]) b.disabled = true;
    status.textContent = "Sending…";
    try {
      const r = await api.post(paths.actions, { action_id: crypto.randomUUID(), seen_version: state.booking.version, ...body });
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

  const book = () => { if (selected !== null) send({ kind: "book", start_utc: isoSeconds(addCells(startUtc, selected)) }); };
  bookBtn.addEventListener("click", book);
  moveBtn.addEventListener("click", book);
  clearBtn.addEventListener("click", () => send({ kind: "clear" }));

  async function refresh() {
    const ticket = sync.startRefresh();
    if (!ticket) return;
    try {
      const fresh = checkDoc(await api.get(paths.get), FORMAT);
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

  const bar = appBar("Team draft");
  bar.classList.add("withaction");
  const editBtn = el("button", { type: "button", class: "secondary small", text: editWeekLabel(usualDoc) });
  editBtn.addEventListener("click", () => { editWeek(usualDoc); });
  bar.append(editBtn);

  root.classList.add("wide");
  root.replaceChildren(bar, heading, ledeEl, bookLine, threadLink, sidesBox, pane);
  render();
}
