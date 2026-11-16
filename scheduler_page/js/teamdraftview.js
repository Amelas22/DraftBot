// Team draft, the view: the two sides, one heatmap of when they are free together with each run of the same people
// boxed and its counts written once, Book / Move / Clear for the matchup's time, and a card saying how the grid counts
// the viewer, whose Everyone | My availability swaps the heatmap for their own this week or usual week. Pressing a
// side or a player leaves them out of the grid until pressed again. Every decision lives in teamdraft.js; this file
// only draws and wires. A round's matchup shows one round (or unnumbered week) at a time, with arrows to the ones
// around it; every read and action names the round being viewed.
import { LinkExpired, checkDoc } from "./api.js";
import { CELL_MS, addCells, isoSeconds, localDateOf } from "./week.js";
import { mountHeatmap, formatCell, layoutWindow, legendKey, weekKeys, zoneName } from "./grid.js";
import { appBar } from "./appbar.js";
import { shownZonePicker } from "./shownzone.js";
import { mountUsual } from "./usual.js";
import { mountThisWeek } from "./thisweek.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { roundLabel, roundQuery } from "./rounds.js";
import { actionErrorText, createSync, hasAvailability, shadeLevel } from "./teams.js";
import {
  FORMAT, bookable, bookingLine, cellState, countText, freeBySide, freeRuns, refusalText, shiftSelection, sideCounts, toggleSide,
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

// deps: { now?: () => Date, paths?: { get, actions }, zonePicker?: { zones?, storage? } } (paths default to the team-draft
// API; a match link passes its own; zonePicker is shownZonePicker's deps). Returns { dispose } which stops the refresh timer.
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
  await showMatchup(root, api, paths, usual, now, cleanups, notice, deps.zonePicker);
  return controller;
}

async function showMatchup(root, api, paths, usualDoc, now, cleanups, notice, zoneDeps) {
  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  // The round being viewed, as the server's query (?round=N or ?week=YYYY-MM-DD); "" is the one the server picks.
  let query = "";
  // The query the drawn state was read with.
  let shownQuery = "";
  let state;
  try {
    state = checkDoc(await api.get(paths.get), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) return notice(expiredMessage());
    throw e;
  }
  // Times are shown in the zone picked at the top (see shownzone.js), at first the viewer's usual-week zone, else the
  // browser's. Picking one draws the heatmap again in it, on the same day tab.
  const picker = shownZonePicker(usualDoc?.zone || Intl.DateTimeFormat().resolvedOptions().timeZone, (z) => {
    zone = z;
    redraw();
    render();
  }, zoneDeps);
  let zone = picker.zone;

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
  const prevBtn = el("button", { type: "button", class: "roundarrow", text: "‹" });
  const nextBtn = el("button", { type: "button", class: "roundarrow", text: "›" });
  const roundText = el("span", { class: "roundlabel" });
  const roundNav = el("div", { class: "roundnav" }, prevBtn, roundText, nextBtn);
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
  const cells = () => state.window.cells;
  const bookedCell = () => {
    if (!state.booking.start_utc) return null;
    const j = (Date.parse(state.booking.start_utc) - startUtc.getTime()) / CELL_MS;
    return Number.isInteger(j) && j >= 0 && j < cells() ? j : null;
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
    const mark = cellState(j, state, now(), startUtc);
    if (mark) classes.push(mark);
    return classes.join(" ");
  };
  // The grid's name for a screen reader: the round it shows, else the rolling default.
  const gridLabel = () => (state.window.shape === "round" && state.round
    ? `${roundLabel(state.round)}, half-hour cells, times in ${zoneName(zone)}` : undefined);
  const makeHeat = (openDate) => mountHeatmap(heatBox, {
    startUtc, zone, openDate, cells: cells(), shape: state.window.shape, gridLabel: gridLabel(), cellClass, cellLabel: label,
    cellText: (j) => (runs[j]?.first ? countText(sideCounts(freeSides, j)) : ""),
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
  // The day a new round opens on: today, when some of it is in view and still ahead (not just the last hours of a
  // round already over), else the first.
  const startDay = () => {
    const today = localDateOf(now(), zone);
    const day = layoutWindow(startUtc, zone, cells(), state.window.shape).find((d) => d.date === today);
    return day && addCells(startUtc, day.cells.at(-1).j + 1) > now() ? today : null;
  };
  let heat = makeHeat(startDay());
  // A new grid for a new zone or window, on the same day tab (or for another round, on startDay); a hover from the
  // old one is dropped.
  const redraw = (openDate = heat.openDate()) => {
    hovered = null;
    if (clicked === null) setReadout(HOVER_HINT);
    heat = makeHeat(openDate);
  };

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
    if (usualNow?.zone) picker.follow(usualNow.zone);
  };
  const toMine = async (w) => {
    if (which !== null) await leave();
    which = w;
    renderCard();
    picker.idle(true);
    everyoneBox.hidden = true;
    mineBox.hidden = false;
    pane.setAttribute("aria-label", "Your availability");
    // This week | Usual week heads the pane; under it a fresh box per editor, so one still loading when the week
    // changes draws into a box no longer shown.
    const box = el("div");
    mineBox.replaceChildren(segmented("Which week", [
      ["This week", w === "thisWeek", () => toMine("thisWeek")], ["Usual week", w === "usual", () => toMine("usual")],
    ]), box);
    editor = (w === "usual" ? mountUsual : mountThisWeek)(box, api, {
      now, usualDoc: usualNow, startUtc, cells: cells(), shape: state.window.shape, onKeptAsUsual: () => toMine("usual"),
    }).catch((e) => {
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
    picker.idle(false);
    mineBox.hidden = true;
    mineBox.replaceChildren();
    everyoneBox.hidden = false;
    pane.setAttribute("aria-label", "Pick a time");
  };

  const renderLegend = () => {
    const swatches = [];
    for (let n = 0; n <= total(); n++) swatches.push(el("i", { class: `s${shadeLevel(n, Math.max(1, total()))}` }));
    const at = now();
    let nobook = false;
    for (let j = 0; j < cells() && !nobook; j++) nobook = cellState(j, state, at, startUtc) === "nobook";
    legend.replaceChildren(el("span", { text: "nobody" }), ...swatches, el("span", { text: `all ${total()} free` }),
      legendKey("run", "same people throughout"), legendKey("booked", "booked"), ...weekKeys(state.window.shape),
      legendKey("past", "already passed"), ...(nobook ? [legendKey("nobook", "can't book here")] : []));
  };

  // The round row: its label, and an arrow to each round around it that exists. Hidden on a rolling window.
  const renderRound = () => {
    const { round } = state;
    roundNav.hidden = round === null;
    if (round === null) return;
    roundText.textContent = roundLabel(round);
    const unit = round.number == null ? "week" : "round";
    prevBtn.setAttribute("aria-label", `Earlier ${unit}`);
    nextBtn.setAttribute("aria-label", `Later ${unit}`);
    prevBtn.hidden = !round.prev;
    nextBtn.hidden = !round.next;
  };
  // An arrow: view that round, and in My availability mount its editor again on the new window.
  const go = async (ref) => {
    if (!ref) return;
    query = roundQuery(ref);
    await refresh();
    if (which !== null) await toMine(which);
  };
  prevBtn.addEventListener("click", () => go(state.round?.prev));
  nextBtn.addEventListener("click", () => go(state.round?.next));

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
    renderRound();
    bookLine.textContent = bookingLine(state, zone);
    threadLink.hidden = !state.matchup.thread_url;
    if (state.matchup.thread_url) threadLink.href = state.matchup.thread_url;
    freeSides = freeBySide(shown(), startUtc);
    const columnStarts = new Set(layoutWindow(startUtc, zone, cells(), state.window.shape).map((d) => d.cells[0].j));
    runs = freeRuns(freeSides, columnStarts, cells());
    ledeEl.textContent = ledeText();
    renderCard();
    renderSides();
    renderLegend();
    renderActions();
    if (clicked !== null && hovered === null) setReadout(whoLabel(clicked));
    heat.update();
  }

  const ledeText = () => {
    const where = `Times in ${zoneName(zone)}.`;
    const showing = total() < everyone().length ? ` Showing ${total()} of ${everyone().length} players.` : "";
    if (!open()) return `This draft has started. ${where}${showing}`;
    return `Each box is a stretch when the same people stay free, with how many on each side at its top. ${where}${showing}`;
  };

  // A finished action (or a failed one) unlocks refreshes again.
  const send = async (body) => {
    const done = sync.startAction();
    for (const b of [bookBtn, moveBtn, clearBtn]) b.disabled = true;
    status.textContent = "Sending…";
    const sentFor = query;
    try {
      const r = await api.post(paths.actions + query, { action_id: crypto.randomUUID(), seen_version: state.booking.version, ...body });
      const fresh = checkDoc(r?.state, FORMAT);
      selected = null;
      // The answer is for the round the action was sent from; once the viewer has moved on, read the one they're on.
      if (sentFor === query) {
        state = fresh;
        render();
      }
      status.textContent = r.ok ? "" : refusalText(r.code);
      if (sentFor !== query) { done(); refresh(); }
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
    const asked = query;
    const moved = asked !== shownQuery;
    try {
      const fresh = checkDoc(await api.get(paths.get + asked), FORMAT);
      // A read for a round the viewer has since left is dropped; the read for the new one follows.
      if (!sync.isCurrent(ticket) || asked !== query) return;
      const was = state.window;
      state = fresh;
      const start = new Date(state.window.start_utc);
      if (start.getTime() !== startUtc.getTime() || was.cells !== state.window.cells || was.shape !== state.window.shape) {
        // A new window (it slid forward, or another round): keep the picked moment, not its cell index.
        selected = shiftSelection(selected, startUtc, start, state.window.cells);
        clicked = shiftSelection(clicked, startUtc, start, state.window.cells);
        startUtc = start;
        redraw(moved ? startDay() : heat.openDate());
      }
      shownQuery = asked;
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
  const bar = appBar("Team draft");
  bar.classList.add("withaction");
  bar.append(picker.row);
  root.replaceChildren(bar, heading, roundNav, ledeEl, bookLine, threadLink, cardBox, sidesBox, pane);
  render();
}
