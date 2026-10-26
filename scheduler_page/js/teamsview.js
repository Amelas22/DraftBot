// Find a team, the view: a rail of teams ranked by shared hours (the viewer's own teams first, with a captain's
// requests), one grid with a Mine / Everyone switch, and the join, captain and open-a-team actions. Every
// decision lives in teams.js; this file only draws and wires.
import { LinkExpired, NeedsUpdate, checkDoc } from "./api.js";
import { decodeCells } from "./week.js";
import { mountUsual } from "./usual.js";
import { mountHeatmap, formatCell } from "./grid.js";
import { appBar } from "./appbar.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { plural, pluralWord } from "./words.js";
import {
  CLOSE_CONFIRM, FORMAT, MULTI_TEAM_WARNING, NOTE_MAX, actionErrorText, cellText, createSync, declinedText, isFull,
  joinNote, joinState, leaveConfirm, membersText, modeAfterSelect, myTeams, noteDrafts, noteError, onOtherTeam, rankTeams, refusalText, requestHeading,
  requestsFor, shadeLevel, slotsText, teamName, windowStart,
} from "./teams.js";

const USUAL_FORMAT = "draftbot.scheduler.usual_week";
const TEAMS_PATH = "/api/teams";
const ACTIONS_PATH = "/api/teams/actions";
const REFRESH_MS = 30000;
const PAINT_FIRST = "Paint your usual week first, so teams can be ranked by how well you fit.";
const HOVER_HINT = "Hover or tap a half-hour to see who's free.";

const hoursShort = (h) => `${h} h`;

// deps: { now?: () => Date }. Returns { dispose } which stops the refresh timer.
export async function mountTeams(root, api, deps = {}) {
  const now = deps.now || (() => new Date());
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
  const list = (doc, firstStatus) => showList(root, api, doc, now, cleanups, notice, firstStatus, editWeek).catch((e) => {
    if (e instanceof LinkExpired) notice(expiredMessage());
    else { console.error(e); notice(actionErrorText(e)); }
  });
  // The list's refresh loop and listeners are disposed before the editor takes the page; showList registers
  // them afresh when the list comes back.
  const editWeek = (doc) => {
    controller.dispose();
    root.classList.remove("wide");
    return mountUsual(root, api, { now, onSaved: (saved) => list(saved, "Saved."), onBack: () => list(doc, "") });
  };
  if (usual === null) {
    await mountUsual(root, api, { now, intro: PAINT_FIRST, onSaved: (doc) => list(doc, "Saved.") });
    return controller;
  }
  await showList(root, api, checkDoc(usual, USUAL_FORMAT), now, cleanups, notice, "", editWeek);
  return controller;
}

async function showList(root, api, usualDoc, now, cleanups, notice, firstStatus, editWeek) {
  root.replaceChildren(el("p", { class: "status", text: "Loading teams…" }));
  let state;
  try {
    state = checkDoc(await api.get(TEAMS_PATH), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) return notice(expiredMessage());
    throw e;
  }

  const zone = usualDoc.zone;
  let startUtc = windowStart(null, now());
  const viewerWeek = () => ({ user: state.me.user, zone, cells: decodeCells(usualDoc.cells) });
  const sync = createSync();
  const drafts = noteDrafts();
  let ranked = [];
  // "t:<team_id>" for a team, "r:<team_id>:<user>" for a request to one of the viewer's teams.
  let selected = null;
  let mode = "everyone";
  let joinKey = "";
  let noteTeam = null;
  // "close:<team_id>" or "leave:<team_id>" while that inline confirm is showing.
  let confirming = null;

  // Persistent pieces: kept across re-renders so typing and focus survive a refresh.
  const rail = el("div", { class: "rail" });
  const railStatus = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const heading = el("h2", { class: "panehead" });
  const mineBtn = el("button", { type: "button", text: "Mine", "aria-pressed": "false" });
  const everyoneBtn = el("button", { type: "button", text: "Everyone", "aria-pressed": "true" });
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Whose times" }, mineBtn, everyoneBtn);
  const heatBox = el("div", { class: "heatbox" });
  const legend = el("div", { class: "legend" });
  const readout = el("p", { class: "readout", "aria-live": "polite", text: HOVER_HINT });
  const join = el("div", { class: "join" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const noteBox = el("textarea", { id: "note", rows: "3", placeholder: "When you're usually free, which seat you like…" });
  noteBox.addEventListener("input", () => { if (noteTeam) drafts.set(noteTeam, noteBox.value); });
  const pane = el("section", { class: "pane", "aria-label": "How you fit" }, heading, seg, heatBox, legend, readout, join, status);

  // Open my own team: an inline form under the bar.
  const openBtn = el("button", { type: "button", class: "secondary small", text: "Open my own team", "aria-expanded": "false" });
  const sizeSelect = el("select", { id: "team-size" }, el("option", { value: "3", text: "3 players" }), el("option", { value: "4", text: "4 players" }));
  const openNote = el("textarea", { id: "team-note", rows: "2", placeholder: "When your team plays, what you're looking for…" });
  const openSubmit = el("button", { type: "button", class: "primary", text: "Open team" });
  const openCancel = el("button", { type: "button", class: "secondary", text: "Cancel" });
  const openWarn = el("p", { class: "warn" });
  const openStatus = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const openForm = el("section", { class: "openform", hidden: "", "aria-label": "Open my own team" },
    el("h2", { text: "Open my own team" }),
    el("label", { class: "field", for: "team-size" }, "Team size ", sizeSelect),
    el("label", { class: "field stack", for: "team-note" }, `Note for players looking (optional, up to ${NOTE_MAX} characters)`, openNote),
    openWarn,
    el("div", { class: "actions" }, openSubmit, openCancel),
    openStatus);

  const bar = appBar("Find a team");
  bar.classList.add("withaction");
  const editBtn = el("button", { type: "button", class: "secondary small", text: "Edit my usual week" });
  editBtn.addEventListener("click", () => { editWeek(usualDoc); });
  bar.append(editBtn, openBtn);
  const lede = el("p", { class: "lede" });
  // Page-level news ("Saved." after a first-run save) stays visible even when there are no teams to show.
  const pageStatus = el("p", { class: "status", role: "status", "aria-live": "polite", text: firstStatus });
  const empty = el("p", { class: "empty", text: "No teams are looking for players right now. Open your own and others can ask to join." });
  const cols = el("div", { class: "cols" }, rail, pane);

  const rankedOf = (teamId) => ranked.find((r) => r.team.team_id === teamId);
  const requestsOf = (teamId) => requestsFor(state, teamId, startUtc);
  // The selection as { team, hours, fit, request? }, or undefined.
  const current = () => {
    if (!selected) return undefined;
    const [kind, teamId, user] = selected.split(":");
    if (kind === "t") return rankedOf(teamId);
    const r = requestsOf(teamId).find((x) => x.request.user === user);
    return r && { team: rankedOf(teamId)?.team, hours: r.hours, fit: r.fit, request: r.request };
  };
  const people = () => {
    const r = current();
    if (!r) return [];
    return mode === "mine" ? r.fit.people.slice(0, 1) : r.fit.people;
  };
  const freeCount = (ps, j) => ps.reduce((n, p) => n + (p.free.has(j) ? 1 : 0), 0);
  const label = (j) => cellText(formatCell(startUtc, zone, j), people(), j);
  const makeHeat = () => mountHeatmap(heatBox, {
    startUtc, zone,
    cellClass: (j) => { const ps = people(); return ps.length ? `s${shadeLevel(freeCount(ps, j), ps.length)}` : ""; },
    cellLabel: label,
    onPick: (j) => { readout.textContent = label(j); },
    onHover: (j) => { readout.textContent = j === null ? HOVER_HINT : label(j); },
  });
  let heat = makeHeat();

  const renderLegend = () => {
    const total = Math.max(1, people().length);
    const swatches = [];
    for (let n = 0; n <= total; n++) swatches.push(el("i", { class: `s${shadeLevel(n, total)}` }));
    const high = total === 1 ? "you're free" : `all ${total} free`;
    legend.replaceChildren(el("span", { text: total === 1 ? "not free" : "nobody" }), ...swatches, el("span", { text: high }));
  };

  // A finished action (or a failed one) unlocks refreshes again; an answer the page can't read is refetched.
  const send = async (body, button, out, onOk) => {
    const done = sync.startAction();
    let refetch = false;
    button.disabled = true;
    out.textContent = "Sending…";
    pageStatus.textContent = "";
    try {
      const r = await api.post(ACTIONS_PATH, { action_id: crypto.randomUUID(), ...body });
      state = checkDoc(r?.state, FORMAT);
      out.textContent = r.ok ? "" : refusalText(r.code);
      if (r.ok) onOk();
      render();
    } catch (e) {
      out.textContent = actionErrorText(e);
      if (e instanceof LinkExpired) cleanups.forEach((c) => c());
      refetch = e instanceof NeedsUpdate;
    } finally {
      done();
      button.disabled = false;
    }
    if (refetch) await refresh();
  };

  const card = ({ team, hours }) => {
    const filled = team.members.length;
    const dots = el("span", { class: "slots", "aria-hidden": "true" });
    for (let i = 0; i < team.size; i++) dots.append(el("i", { class: i < filled ? "" : "open" }));
    const js = joinState(state, team);
    const others = membersText(team, state.me.user);
    const tag = js.kind === "member" ? "Your team" : js.kind === "requested" ? "Requested" : js.kind === "full" ? "Full" : js.kind === "paused" ? "Paused" : "";
    const key = `t:${team.team_id}`;
    const b = el("button", { type: "button", class: "card", "data-sel": key, "data-focus": `card:${team.team_id}`, "aria-pressed": String(key === selected) },
      el("span", { class: "row" }, el("b", { text: teamName(team) }), el("span", { class: "fit", text: hoursShort(hours), title: `${plural(hours, "hour")} a week together` })),
      el("span", { class: "meta" }, dots, ` ${slotsText(team)}`, ...(tag ? [" · ", el("span", { class: "tag", text: tag })] : [])),
      ...(others ? [el("span", { class: "members", text: others })] : []),
      ...(team.note ? [el("span", { class: "note", text: team.note })] : []));
    b.addEventListener("click", () => select(key));
    return b;
  };

  // One pending request: pick it to see the grid with them added, or answer it.
  const requestRow = (team, { request, hours }) => {
    const key = `r:${team.team_id}:${request.user}`;
    const pick = el("button", { type: "button", class: "reqpick", "data-sel": key, "data-focus": `pick:${key}`, "aria-pressed": String(key === selected) },
      el("span", { class: "row" }, el("b", { text: request.name }), el("span", { class: "fit", text: hoursShort(hours), title: `${plural(hours, "hour")} a week with your team` })),
      ...(request.note ? [el("span", { class: "quote", text: `“${request.note}”` })] : []));
    pick.addEventListener("click", () => select(key));
    const accept = el("button", { type: "button", class: "primary small", text: "Accept", "data-focus": `accept:${key}` });
    const decline = el("button", { type: "button", class: "secondary small", text: "Decline", "data-focus": `decline:${key}` });
    const args = { team_id: team.team_id, requester: request.user };
    accept.addEventListener("click", () => send({ kind: "accept", ...args }, accept, railStatus, () => {
      railStatus.textContent = `${request.name} is on your team. They get a DM.`;
    }));
    decline.addEventListener("click", () => send({ kind: "decline", ...args }, decline, railStatus, () => {
      railStatus.textContent = `Declined ${request.name}. They get a DM.`;
    }));
    return el("div", { class: "req" }, pick, el("div", { class: "actions" }, accept, decline));
  };

  // An inline "are you sure": the words, the action, and the way out.
  const confirmBox = (team, kind, text, yesText, noText, body, okText) => {
    const yes = el("button", { type: "button", class: "primary small", text: yesText, "data-focus": `${kind}-yes:${team.team_id}` });
    const no = el("button", { type: "button", class: "secondary small", text: noText, "data-focus": `${kind}-no:${team.team_id}` });
    yes.addEventListener("click", () => send(body, yes, railStatus, () => { confirming = null; railStatus.textContent = okText; }));
    no.addEventListener("click", () => { confirming = null; renderRail(`${kind}:${team.team_id}`); });
    return el("div", { class: "confirm", role: "group", "aria-label": yesText }, el("p", { text }), el("div", { class: "actions" }, yes, no));
  };

  const askFirst = (kind, team, text) => {
    const b = el("button", { type: "button", class: "secondary small", text, "data-focus": `${kind}:${team.team_id}` });
    b.addEventListener("click", () => { confirming = `${kind}:${team.team_id}`; railStatus.textContent = ""; renderRail(`${kind}-no:${team.team_id}`); });
    return b;
  };

  const captainTools = (team) => {
    const kids = [];
    if (isFull(team)) {
      kids.push(el("p", { class: "ready" }, "Your team is full. Register it with ", el("code", { text: "/tournament register" }), " in Discord."));
    } else {
      const reqs = requestsOf(team.team_id);
      kids.push(el("h3", { text: `Requests (${reqs.length})` }));
      if (reqs.length) kids.push(...reqs.map((r) => requestRow(team, r)));
      else kids.push(el("p", { class: "meta", text: "No requests yet. You get a DM when someone asks to join." }));
      const on = team.taking_requests;
      const sw = el("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(on), "data-focus": `switch:${team.team_id}` },
        el("span", { class: "track", "aria-hidden": "true" }), "Taking requests");
      sw.addEventListener("click", () => send({ kind: "set_taking_requests", team_id: team.team_id, on: !on }, sw, railStatus, () => {
        railStatus.textContent = on ? "Paused. Players can see your team but can't ask to join." : "Players can ask to join again.";
      }));
      kids.push(sw);
    }
    kids.push(confirming === `close:${team.team_id}`
      ? confirmBox(team, "close", CLOSE_CONFIRM, "Close team", "Keep it", { kind: "close_team", team_id: team.team_id }, "Your team is closed.")
      : el("div", { class: "actions" }, askFirst("close", team, "Close team")));
    return kids;
  };

  const memberTools = (team) => [confirming === `leave:${team.team_id}`
    ? confirmBox(team, "leave", leaveConfirm(team), "Leave", "Stay", { kind: "leave_team", team_id: team.team_id }, `You left ${teamName(team)}.`)
    : el("div", { class: "actions" }, askFirst("leave", team, "Leave team"))];

  // focusAfter: the data-focus key to focus once drawn (after a confirm opens or closes).
  function renderRail(focusAfter) {
    const active = document.activeElement;
    const focused = active?.closest?.(".rail [data-focus]")?.dataset.focus;
    const focusedTeam = active?.closest?.(".rail [data-team]")?.dataset.team;
    const own = myTeams(state);
    const ownIds = new Set(own.map((t) => t.team_id));
    const kids = [];
    if (own.length) {
      kids.push(el("h2", { text: own.length > 1 ? "My teams" : "My team" }));
      for (const team of own) {
        const r = rankedOf(team.team_id);
        const tools = team.captain === state.me.user ? captainTools(team) : memberTools(team);
        kids.push(el("div", { class: "mine", "data-team": team.team_id }, card(r), ...tools));
      }
    }
    const others = ranked.filter((r) => !ownIds.has(r.team.team_id));
    if (others.length) kids.push(el("h2", { text: own.length ? "Other teams" : "Teams" }), ...others.map(card));
    kids.push(railStatus);
    rail.replaceChildren(...kids);
    const want = focusAfter ?? focused;
    if (!want) return;
    const target = rail.querySelector(`[data-focus="${CSS.escape(want)}"]`)
      // The thing that had focus is gone (an answered request): land on that team's card.
      ?? (focusedTeam ? rail.querySelector(`[data-focus="${CSS.escape(`card:${focusedTeam}`)}"]`) : null);
    target?.focus();
  }

  const renderJoin = () => {
    const r = current();
    if (!r) { join.replaceChildren(); joinKey = ""; noteTeam = null; return; }
    const { team } = r;
    const js = joinState(state, team);
    const key = `${selected}|${js.kind}|${Boolean(js.declined)}|${isFull(team)}|${onOtherTeam(state)}`;
    if (key === joinKey) return;
    joinKey = key;
    noteTeam = null;
    const kids = [];
    if (r.request) {
      kids.push(el("p", { class: "meta", text: `Accept or decline ${r.request.name} in your list of requests.` }));
    } else if (js.kind === "open") {
      noteTeam = team.team_id;
      noteBox.value = drafts.get(team.team_id);
      const ask = el("button", { type: "button", class: "primary", text: "Request to join" });
      ask.addEventListener("click", () => {
        const note = drafts.get(team.team_id);
        const err = noteError(note);
        if (err) { status.textContent = err; return; }
        send({ kind: "request_join", team_id: team.team_id, note }, ask, status, () => {
          drafts.clear(team.team_id);
          if (noteTeam === team.team_id) noteBox.value = "";
          status.textContent = `Sent. You'll get a DM when ${team.captain_name} answers.`;
        });
      });
      if (js.declined) kids.push(el("p", { class: "meta", text: declinedText(team) }));
      kids.push(el("label", { class: "field stack", for: "note" }, `Note to the captain (optional, up to ${NOTE_MAX} characters)`, noteBox));
      if (onOtherTeam(state)) kids.push(el("p", { class: "warn", text: MULTI_TEAM_WARNING }));
      kids.push(el("div", { class: "actions" }, ask, el("span", { class: "meta", text: `${team.captain_name} gets a DM and sees your request in their list.` })));
    } else if (js.kind === "requested") {
      const withdraw = el("button", { type: "button", class: "secondary", text: "Withdraw" });
      withdraw.addEventListener("click", () => send({ kind: "withdraw_request", team_id: team.team_id }, withdraw, status, () => {
        status.textContent = "Request withdrawn.";
      }));
      kids.push(el("div", { class: "actions" }, el("button", { type: "button", class: "primary", text: "Requested", disabled: "" }), withdraw));
    } else {
      const text = joinNote(js, team);
      if (text) kids.push(el("p", { class: "meta", text }));
    }
    join.replaceChildren(...kids);
  };

  const renderPane = () => {
    const r = current();
    if (!r) return;
    const own = joinState(state, r.team).kind === "member";
    heading.textContent = r.request ? requestHeading(r.request, r.hours)
      : own ? `Your team: ${plural(r.hours, "hour")} a week when you're all free`
        : `You and ${teamName(r.team)}: ${r.hours} ${pluralWord(r.hours, "hour")} a week`;
    mineBtn.setAttribute("aria-pressed", String(mode === "mine"));
    everyoneBtn.setAttribute("aria-pressed", String(mode === "everyone"));
    renderLegend();
    heat.update();
    renderJoin();
  };

  const select = (key) => {
    if (key !== selected) { status.textContent = ""; readout.textContent = HOVER_HINT; }
    selected = key;
    mode = modeAfterSelect(key, mode);
    for (const b of rail.querySelectorAll("[data-sel]")) b.setAttribute("aria-pressed", String(b.dataset.sel === key));
    renderPane();
  };

  function render() {
    ranked = rankTeams(viewerWeek(), state.teams, startUtc);
    if (!current()) {
      // An answered request falls back to its team; anything else to the viewer's own team, then the best fit.
      const teamId = selected?.split(":")[1];
      const fallback = rankedOf(teamId)?.team ?? myTeams(state)[0] ?? ranked[0]?.team;
      selected = fallback ? `t:${fallback.team_id}` : null;
    }
    if (confirming && !state.teams.some((t) => t.team_id === confirming.split(":")[1])) confirming = null;
    lede.textContent = `Teams ranked by the hours you're all usually free over the next 7 days. Times in ${zone.replaceAll("_", " ")}.`;
    cols.hidden = ranked.length === 0;
    empty.hidden = ranked.length > 0;
    renderRail();
    renderPane();
  }

  mineBtn.addEventListener("click", () => { mode = "mine"; renderPane(); });
  everyoneBtn.addEventListener("click", () => { mode = "everyone"; renderPane(); });

  const setOpen = (open) => {
    openForm.hidden = !open;
    openBtn.setAttribute("aria-expanded", String(open));
    openWarn.textContent = onOtherTeam(state) ? MULTI_TEAM_WARNING : "";
    openWarn.hidden = !onOtherTeam(state);
    openStatus.textContent = "";
    if (open) sizeSelect.focus();
  };
  openBtn.addEventListener("click", () => setOpen(openForm.hidden));
  openCancel.addEventListener("click", () => { setOpen(false); openBtn.focus(); });
  openSubmit.addEventListener("click", () => {
    const err = noteError(openNote.value);
    if (err) { openStatus.textContent = err; return; }
    const before = new Set(state.teams.map((t) => t.team_id));
    send({ kind: "open_team", size: Number(sizeSelect.value), note: openNote.value }, openSubmit, openStatus, () => {
      const mine = state.teams.find((t) => !before.has(t.team_id) && t.captain === state.me.user);
      if (mine) selected = `t:${mine.team_id}`;
      openNote.value = "";
      openForm.hidden = true;
      openBtn.setAttribute("aria-expanded", "false");
      status.textContent = "Your team is open. Players can now ask to join.";
    });
  });

  // Refetch while the page is visible, and as soon as it becomes visible again. A refresh never starts during
  // an action and its answer is dropped if an action finished meanwhile; the 7-day window follows the clock.
  async function refresh() {
    const ticket = sync.startRefresh();
    if (!ticket) return;
    try {
      const fresh = checkDoc(await api.get(TEAMS_PATH), FORMAT);
      if (!sync.isCurrent(ticket)) return;
      state = fresh;
      const start = windowStart(startUtc, now());
      if (start !== startUtc) { startUtc = start; heat = makeHeat(); readout.textContent = HOVER_HINT; }
      render();
    } catch (e) {
      if (e instanceof LinkExpired) notice(expiredMessage());
      else console.warn("teams refresh failed", e);
    }
  }
  const timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, REFRESH_MS);
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  cleanups.push(() => clearInterval(timer), () => document.removeEventListener("visibilitychange", onVisible));

  root.classList.add("wide");
  root.replaceChildren(bar, openForm, el("h1", { text: "Find a team" }), lede, pageStatus, empty, cols);
  render();
}
