// Team draft, the view: the two sides, one heatmap of when they are free together with each run of the same people
// boxed and its counts written once, Book / Move / Clear for the matchup's time, and a card saying how the grid counts
// the viewer, whose Everyone | My availability swaps the heatmap for their own this week or usual week. Pressing a
// side or a player leaves them out of the grid until pressed again. Every decision lives in teamdraft.js; this file
// only draws and wires.
import { LinkExpired, checkDoc } from "./api.js";
import { CELLS, CELL_MS, addCells, isoSeconds } from "./week.js";
import { mountHeatmap, formatCell, layoutWindow } from "./grid.js";
import { appBar } from "./appbar.js";
import { mountUsual } from "./usual.js";
import { mountThisWeek } from "./thisweek.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { actionErrorText, createSync, hasAvailability, shadeLevel } from "./teams.js";
import {
  FORMAT, bookable, bookingLine, countText, freeBySide, freeRuns, refusalText, shiftSelection, sideCounts, toggleSide,
  togglePerson, whoIsFree, whoText, withoutPeople,
} from "./teamdraft.js";

const TEAM_DRAFT_PATHS = { get: "/api/team-draft", actions: "/api/team-draft/actions" };
const REFRESH_MS = 30000;
const HOVER_HINT = "Hover or tap a half-hour to see who's free on each side. Tap a time to pick it.";
const TOGGLE_HINT = "Tap a team or a player to leave them out of the grid; tap again to bring them back.";
const NO_WEEK = "no availability entered";
// The card's words in My availability, per week being changed.
const MINE_TEXT = {
  thisWeek: { title: "Changing this week only", sub: "Just these days, for this matchup and every other." },
  usual: { title: "Changing your usual week", sub: "It repeats every week you haven't changed." },
};

// The viewer's card on Everyone: how the grid counts them. me is their place on a side, or undefined.
export function everyoneText(me) {
  if (!me) return { tone: "", title: "Your availability", sub: "You aren't on either side of this matchup." };
  if (!hasAvailability(me)) {
    return { tone: "warn", title: "The grid can't count you yet",
      sub: "You haven't entered when you're free, so every half-hour shows you as not free." };
  }
  if (me.overrides?.length) return { tone: "", title: "You're counted from this week's changes", sub: "Your usual week, with the days you changed this week." };
  return { tone: "", title: "You're counted from your usual week", sub: "Busy one night? Open My availability to change this week only." };
}

// A row of pressable choices, [text, on, go] each; pressing the one already on does nothing.
const segmented = (label, items) => el("div", { class: "seg", role: "group", "aria-label": label }, ...items.map(([text, on, go]) => {
  const b = el("button", { type: "button", "aria-pressed": String(on), text });
  b.addEventListener("click", () => { if (!on) go(); });
  return b;
}));

// The card: its words, and Everyone | My availability (which is null on Everyone, else the week being changed).
// Every view's words are laid in the same spot and only the current one shows, so the card is as tall as its longest
// words in every view and switching never moves the pane below it.
function statusCard({ everyoneWords, which, onEveryone, onMine }) {
  const views = [[null, everyoneWords], ...Object.entries(MINE_TEXT).map(([w, words]) => [w, { ...words, tone: "mine" }])];
  const tone = views.find(([w]) => w === which)[1].tone;
  return el("section", { class: `mecard ${tone}`.trim(), "aria-label": "Your availability" },
    el("div", { class: "mehead" },
      el("div", { class: "metext" }, ...views.map(([w, { title, sub }]) => el("div", { class: "words", "aria-hidden": String(w !== which) },
        el("strong", { text: title }), el("span", { text: sub })))),
      segmented("Show", [["Everyone", which === null, onEveryone], ["My availability", which !== null, onMine]])));
}

// deps: { now?: () => Date, paths?: { get, actions } } (paths default to the team-draft API; a match link passes its own). Returns { dispose } which stops the refresh timer.
export async function mountTeamDraft(root, api, deps = {}) {
  const now = deps.now || (() => new Date());
  const paths = deps.paths || TEAM_DRAFT_PATHS;
  const cleanups = [];
  const controller = { dispose: () => { while (cleanups.length) cleanups.pop()(); } };
  const notice = (text) => { controller.dispose(); root.classList.remove("wide"); root.replaceChildren(el("p", { class: "notice", text })); };

  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  let usual;
  try {
    usual = await api.getUsual();
  } catch (e) {
    if (e instanceof LinkExpired) { notice(expiredMessage()); return controller; }
    throw e;
  }
  await showMatchup(root, api, paths, usual, now, cleanups, notice);
  return controller;
}

async function showMatchup(root, api, paths, usualDoc, now, cleanups, notice) {
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
  // The cell last clicked, whose names the readout shows when the pointer is not on the grid.
  let clicked = null;
  // The cell a mouse is over, so a redraw leaves its counts alone; touch and keyboard focus never hold it.
  let hovered = null;
  // User ids the viewer has switched off; kept across refreshes.
  let off = new Set();
  // The week the viewer is changing in My availability, or null on Everyone; and that week's editor, once mounted.
  let which = null;
  let editor = Promise.resolve(null);
  // The viewer's usual week, re-read after they change it.
  let usualNow = usualDoc;

  const heading = el("h1");
  const ledeEl = el("p", { class: "lede" });
  const bookLine = el("p", { class: "bookline", role: "status", "aria-live": "polite" });
  const threadLink = el("a", { class: "threadlink", text: "Open the thread", target: "_blank", rel: "noopener" });
  const cardBox = el("div");
  const sidesBox = el("div", { class: "sides" });
  const heatBox = el("div", { class: "heatbox" });
  const legend = el("div", { class: "legend" });
  const readout = el("p", { class: "readout", "aria-live": "polite", text: HOVER_HINT });
  const bookBtn = el("button", { type: "button", class: "primary", text: "Book this time" });
  const moveBtn = el("button", { type: "button", class: "primary", text: "Move here" });
  const clearBtn = el("button", { type: "button", class: "secondary", text: "Clear" });
  const actions = el("div", { class: "actions" }, bookBtn, moveBtn, clearBtn);
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  // Everyone and My availability share the pane: one is hidden, so the page around it never moves.
  const everyoneBox = el("div", { class: "everyone" }, heatBox, legend, readout, actions, status);
  const mineBox = el("div", { class: "minehost" });
  mineBox.hidden = true;
  const pane = el("section", { class: "pane", "aria-label": "Pick a time" }, everyoneBox, mineBox);

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
  const whoLabel = (j) => `${formatCell(startUtc, zone, j)}: ${whoText(whoIsFree(shown(), freeSides, j))}`;
  // Written only when it changes, so the polite live region does not re-announce an unchanged readout.
  const setReadout = (text) => { if (readout.textContent !== text) readout.textContent = text; };
  const restingReadout = () => (clicked === null ? HOVER_HINT : whoLabel(clicked));
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
      clicked = j;
      hovered = null;
      setReadout(whoLabel(j));
      renderActions();
      heat.update();
    },
    onHover: (j, byMouse) => { hovered = byMouse ? j : null; setReadout(j === null ? restingReadout() : label(j)); },
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
          if (!hasAvailability(p)) li.append(el("span", { class: "noweek", text: ` (${NO_WEEK})` }));
          return li;
        })));
    }));
  };

  const me = () => everyone().find((p) => p.user === state.me?.user);
  const renderCard = () => {
    cardBox.replaceChildren(statusCard({ everyoneWords: everyoneText(me()), which, onEveryone: toEveryone, onMine: () => toMine(firstWeek()) }));
  };
  // My availability opens on this week, or on the usual week for a viewer with nothing entered yet.
  const firstWeek = () => (me() && !hasAvailability(me()) ? "usual" : "thisWeek");

  // The editors save as the player paints. Leaving one lets its last save land first, then re-reads the usual week
  // (a save may have written it) for the next editor, and the matchup for Everyone.
  const leave = async () => {
    await (await editor)?.flush();
    usualNow = await api.getUsual().catch(() => usualNow);
  };
  const toMine = async (w) => {
    if (which !== null) await leave();
    which = w;
    renderCard();
    everyoneBox.hidden = true;
    mineBox.hidden = false;
    pane.setAttribute("aria-label", "Your availability");
    // This week | Usual week heads the pane; under it a fresh box per editor, so one still loading when the week
    // changes draws into a box no longer shown.
    const box = el("div");
    mineBox.replaceChildren(segmented("Which week", [
      ["This week", w === "thisWeek", () => toMine("thisWeek")], ["Usual week", w === "usual", () => toMine("usual")],
    ]), box);
    editor = (w === "usual" ? mountUsual : mountThisWeek)(box, api, { now, usualDoc: usualNow, startUtc, onKeptAsUsual: () => toMine("usual") }).catch((e) => {
      if (e instanceof LinkExpired) notice(expiredMessage());
      else { console.error(e); box.replaceChildren(el("p", { class: "status", text: actionErrorText(e) })); }
      return null;
    });
  };
  const toEveryone = async () => {
    await leave();
    editor = Promise.resolve(null);
    await refresh();
    which = null;
    renderCard();
    mineBox.hidden = true;
    mineBox.replaceChildren();
    everyoneBox.hidden = false;
    pane.setAttribute("aria-label", "Pick a time");
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
    renderCard();
    renderSides();
    renderLegend();
    renderActions();
    if (clicked !== null && hovered === null) setReadout(whoLabel(clicked));
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
        selected = shiftSelection(selected, startUtc, start);
        clicked = shiftSelection(clicked, startUtc, start);
        hovered = null;
        if (clicked === null) setReadout(HOVER_HINT);
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
  root.replaceChildren(appBar("Team draft"), heading, ledeEl, bookLine, threadLink, cardBox, sidesBox, pane);
  render();
}
