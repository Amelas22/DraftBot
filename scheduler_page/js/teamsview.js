// Find a team, the view: a rail of teams ranked by shared hours, one grid with a Mine / Everyone switch, and
// the join and open-a-team actions. Every decision lives in teams.js; this file only draws and wires.
import { LinkExpired, checkDoc } from "./api.js";
import { decodeCells } from "./week.js";
import { mountUsual } from "./usual.js";
import { mountHeatmap, formatCell } from "./grid.js";
import { appBar } from "./appbar.js";
import { el } from "./dom.js";
import { expiredMessage } from "./messages.js";
import { plural, pluralWord } from "./words.js";
import {
  FORMAT, MULTI_TEAM_WARNING, NOTE_MAX, actionErrorText, cellText, declinedText, halfHourFloor, joinState, noteError,
  onOtherTeam, rankTeams, refusalText, shadeLevel, teamName,
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
  if (usual === null) {
    await mountUsual(root, api, {
      now,
      intro: PAINT_FIRST,
      onSaved: (doc) => {
        showList(root, api, doc, now, cleanups, notice, "Saved.").catch((e) => {
          if (e instanceof LinkExpired) notice(expiredMessage());
          else { console.error(e); notice(actionErrorText(e)); }
        });
      },
    });
    return controller;
  }
  await showList(root, api, checkDoc(usual, USUAL_FORMAT), now, cleanups, notice, "");
  return controller;
}

async function showList(root, api, usualDoc, now, cleanups, notice, firstStatus) {
  root.replaceChildren(el("p", { class: "status", text: "Loading teams…" }));
  let state;
  try {
    state = checkDoc(await api.get(TEAMS_PATH), FORMAT);
  } catch (e) {
    if (e instanceof LinkExpired) return notice(expiredMessage());
    throw e;
  }

  const zone = usualDoc.zone;
  const startUtc = halfHourFloor(now());
  const viewerWeek = () => ({ user: state.me.user, zone, cells: decodeCells(usualDoc.cells) });
  let ranked = [];
  let selected = null;
  let mode = "everyone";
  let joinKey = "";

  // Persistent pieces: kept across re-renders so typing and focus survive a refresh.
  const rail = el("div", { class: "rail" });
  const heading = el("h2", { class: "panehead" });
  const mineBtn = el("button", { type: "button", text: "Mine", "aria-pressed": "false" });
  const everyoneBtn = el("button", { type: "button", text: "Everyone", "aria-pressed": "true" });
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Whose times" }, mineBtn, everyoneBtn);
  const heatBox = el("div", { class: "heatbox" });
  const legend = el("div", { class: "legend" });
  const readout = el("p", { class: "readout", "aria-live": "polite", text: HOVER_HINT });
  const join = el("div", { class: "join" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite", text: firstStatus });
  const noteBox = el("textarea", { id: "note", rows: "3", placeholder: "When you're usually free, which seat you like…" });
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
  bar.append(openBtn);
  const lede = el("p", { class: "lede" });
  const empty = el("p", { class: "empty", text: "No teams are looking for players right now. Open your own and others can ask to join." });
  const cols = el("div", { class: "cols" }, rail, pane);

  const current = () => ranked.find((r) => r.team.team_id === selected);
  const people = () => {
    const r = current();
    if (!r) return [];
    return mode === "mine" ? r.fit.people.slice(0, 1) : r.fit.people;
  };
  const freeCount = (ps, j) => ps.reduce((n, p) => n + (p.free.has(j) ? 1 : 0), 0);
  const heat = mountHeatmap(heatBox, {
    startUtc, zone,
    cellClass: (j) => { const ps = people(); return ps.length ? `s${shadeLevel(freeCount(ps, j), ps.length)}` : ""; },
    cellLabel: (j) => cellText(formatCell(startUtc, zone, j), people(), j),
    onPick: (j) => { readout.textContent = cellText(formatCell(startUtc, zone, j), people(), j); },
    onHover: (j) => { readout.textContent = j === null ? HOVER_HINT : cellText(formatCell(startUtc, zone, j), people(), j); },
  });

  const renderLegend = () => {
    const total = Math.max(1, people().length);
    const swatches = [];
    for (let n = 0; n <= total; n++) swatches.push(el("i", { class: `s${shadeLevel(n, total)}` }));
    const high = total === 1 ? "you're free" : `all ${total} free`;
    legend.replaceChildren(el("span", { text: total === 1 ? "not free" : "nobody" }), ...swatches, el("span", { text: high }));
  };

  const card = ({ team, hours }) => {
    const filled = team.members.length;
    const dots = el("span", { class: "slots", "aria-hidden": "true" });
    for (let i = 0; i < team.size; i++) dots.append(el("i", { class: i < filled ? "" : "open" }));
    const js = joinState(state, team);
    const tag = js.kind === "member" ? "Your team" : js.kind === "requested" ? "Requested" : js.kind === "full" ? "Full" : js.kind === "paused" ? "Paused" : "";
    const b = el("button", { type: "button", class: "card", "data-id": team.team_id, "aria-pressed": String(team.team_id === selected) },
      el("span", { class: "row" }, el("b", { text: teamName(team) }), el("span", { class: "fit", text: hoursShort(hours), title: `${plural(hours, "hour")} a week together` })),
      el("span", { class: "meta" }, dots, ` ${filled} of ${team.size}`, ...(tag ? [" · ", el("span", { class: "tag", text: tag })] : [])),
      ...(team.note ? [el("span", { class: "note", text: team.note })] : []));
    b.addEventListener("click", () => select(team.team_id));
    return b;
  };

  const renderRail = () => {
    const focusedId = document.activeElement?.closest?.(".card")?.dataset.id;
    rail.replaceChildren(el("h2", { text: "Teams" }), ...ranked.map(card));
    if (focusedId) rail.querySelector(`.card[data-id="${CSS.escape(focusedId)}"]`)?.focus();
  };

  const send = async (body, button, out, onOk) => {
    button.disabled = true;
    out.textContent = "Sending…";
    try {
      const r = await api.post(ACTIONS_PATH, { action_id: crypto.randomUUID(), ...body });
      state = checkDoc(r.state, FORMAT);
      out.textContent = r.ok ? "" : refusalText(r.code);
      if (r.ok) onOk();
      render();
    } catch (e) {
      out.textContent = actionErrorText(e);
      if (e instanceof LinkExpired) cleanups.forEach((c) => c());
    } finally {
      button.disabled = false;
    }
  };

  const renderJoin = () => {
    const r = current();
    if (!r) { join.replaceChildren(); joinKey = ""; return; }
    const { team } = r;
    const js = joinState(state, team);
    const key = `${team.team_id}|${js.kind}|${Boolean(js.declined)}|${onOtherTeam(state)}`;
    if (key === joinKey) return;
    joinKey = key;
    const kids = [];
    if (js.kind === "open") {
      const ask = el("button", { type: "button", class: "primary", text: "Request to join" });
      ask.addEventListener("click", () => {
        const err = noteError(noteBox.value);
        if (err) { status.textContent = err; return; }
        send({ kind: "request_join", team_id: team.team_id, note: noteBox.value }, ask, status, () => {
          noteBox.value = "";
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
      const text = js.kind === "member" ? (js.captain ? "This is your team. You get a DM when someone asks to join." : "You're on this team.")
        : js.kind === "full" ? "This team is full." : "This team isn't taking requests right now.";
      kids.push(el("p", { class: "meta", text }));
    }
    join.replaceChildren(...kids);
  };

  const renderPane = () => {
    const r = current();
    if (!r) return;
    const own = joinState(state, r.team).kind === "member";
    heading.textContent = own
      ? `Your team: ${plural(r.hours, "hour")} a week when you're all free`
      : `You and ${teamName(r.team)}: ${r.hours} ${pluralWord(r.hours, "hour")} a week`;
    mineBtn.setAttribute("aria-pressed", String(mode === "mine"));
    everyoneBtn.setAttribute("aria-pressed", String(mode === "everyone"));
    renderLegend();
    heat.update();
    renderJoin();
  };

  const select = (id) => {
    if (id !== selected) { status.textContent = ""; readout.textContent = HOVER_HINT; }
    selected = id;
    for (const b of rail.querySelectorAll(".card")) b.setAttribute("aria-pressed", String(b.dataset.id === id));
    renderPane();
  };

  function render() {
    ranked = rankTeams(viewerWeek(), state.teams, startUtc);
    if (!ranked.some((r) => r.team.team_id === selected)) selected = ranked[0]?.team.team_id ?? null;
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
      if (mine) selected = mine.team_id;
      openNote.value = "";
      openForm.hidden = true;
      openBtn.setAttribute("aria-expanded", "false");
      status.textContent = "Your team is open. Players can now ask to join.";
    });
  });

  // Refetch while the page is visible, and as soon as it becomes visible again.
  const refresh = async () => {
    try {
      state = checkDoc(await api.get(TEAMS_PATH), FORMAT);
      render();
    } catch (e) {
      if (e instanceof LinkExpired) notice(expiredMessage());
      else console.warn("teams refresh failed", e);
    }
  };
  const timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, REFRESH_MS);
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  cleanups.push(() => clearInterval(timer), () => document.removeEventListener("visibilitychange", onVisible));

  root.classList.add("wide");
  root.replaceChildren(bar, openForm, el("h1", { text: "Find a team" }), lede, empty, cols);
  render();
}
