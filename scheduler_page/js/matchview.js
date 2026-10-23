// The league match view: who's free over the match week, the best draft hours, and the Book request.
// The model lives in match.js and the request flow in request.js; this file only draws and wires them.
import {
  parseShared, parseMe, copyNeeded, freeCount, draftCheck, bestTimes, closestMisses, timeLabel,
  draftRequest, draftBooked, nobodyFollows, duration, REFUSALS,
} from "./match.js";
import { CELL_MS, instantOf, cellOfIso, isoSeconds, hasRun } from "./week.js";
import { mountHeatmap, formatClock } from "./grid.js";
import { createRequester, requestStatus, sendErrorText } from "./request.js";
import { LinkExpired, NeedsUpdate } from "./api.js";
import { expiredMessage } from "./messages.js";
import { el } from "./dom.js";
import { plural, andList } from "./words.js";
import { fakeOutcome } from "./later.js";
import { mountLater, laterTabShown, defaultTab, pointsToLater } from "./laterview.js";

const MATCH_EXPIRED = expiredMessage("match");
const REFRESH_MS = 60000;

const zoneAbbrev = (date, zone) =>
  new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(date)
    .find((p) => p.type === "timeZoneName")?.value || zone;
const discordTime = (date) => `<t:${Math.floor(date.getTime() / 1000)}:f>`;

// The one sentence under the header about what follows the draft, or null when there is nothing to say.
// Nobody following is said whether or not the server has a library; the copy sentences need one.
export function libraryLine(shared) {
  if (nobodyFollows(shared)) {
    return draftBooked(shared)
      ? "Nobody plays right after the draft: each of you books your games under Later games."
      : "Nobody plays right after the draft. Once it's booked, each of you books your games under Later games.";
  }
  if (!shared.library || !shared.library.enabled) return null;
  const copy = copyNeeded(shared);
  if (!copy.needed) return "No library copy is needed: nobody playing right after the draft needs one.";
  // copy_offset_cells counts from the draft's start, so the gap after the draft is what's left past draft_cells.
  const { copy_offset_cells: offset, copy_cells: n, draft_cells: dc } = shared.rules;
  const gap = offset - dc;
  const when = gap > 0 ? `${duration(gap)} after the draft ends, for ${duration(n)}` : `for ${duration(n)} from the end of the draft`;
  return `Booking holds one league copy ${when}, for ${andList(copy.who)} (they play right after and need the library).`;
}

// deps: { getJson, putJson, now?: () => Date, zone?: string }. Resolves to { dispose() }, which stops every
// timer and listener; the view calls it itself before showing an error notice.
export async function mountMatch(root, link, deps) {
  const zone = deps.zone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const notice = (text) => root.replaceChildren(el("p", { class: "notice", text }));
  const noop = { dispose() {} };

  const fetchMe = async () => parseMe(await deps.getJson(link.urls.me_get));
  const fetchBoth = async () => {
    const [s, m] = await Promise.all([deps.getJson(link.urls.shared_get), fetchMe()]);
    return { shared: parseShared(s), me: m };
  };

  root.replaceChildren(el("p", { class: "status", text: "Loading the match…" }));
  let shared, me;
  try {
    ({ shared, me } = await fetchBoth());
  } catch (e) {
    if (e instanceof LinkExpired) { notice(MATCH_EXPIRED); return noop; }
    if (e instanceof NeedsUpdate) { notice(e.message); return noop; }
    throw e;
  }

  // Fixture walkthroughs run at the moment the files were made, so every scenario reads the same.
  const fixtureNow = new Date(shared.generated_at);
  const now = link.fixture ? () => fixtureNow : (deps.now || (() => new Date()));
  const startUtc = new Date(shared.window.start_utc);
  const instant = (j) => instantOf(shared, j);
  const zoneName = zoneAbbrev(startUtc, zone);
  const viewer = link.user;

  const off = new Set();
  let picked = null;
  let sendError = null;
  let copyBox = null; // text shown for manual copy when the clipboard refuses
  let copyBoxNew = false; // focus and select the box only when it first appears
  let copied = false;
  let disposed = false;
  const cleanups = [];

  const players = () => shared.teams.flatMap((t, ti) => t.players.map((p) => ({ ...p, team: ti })));
  const viewerTeam = shared.teams.findIndex((t) => t.players.some((p) => p.user === viewer));
  const otherTeam = shared.teams[viewerTeam === 0 ? 1 : viewerTeam === 1 ? 0 : 1];

  // Who on each team is free for a whole draft starting at j (filters applied).
  const freeFor = (j) => shared.teams.map((t) => t.players.filter((p) =>
    !off.has(p.user) && hasRun(p.cells, j, shared.rules.draft_cells)).map((p) => p.name));

  const requester = createRequester({
    putUrl: link.urls.request_put,
    put: deps.putJson,
    fixture: link.fixture,
    // The draft and the later games share this one requester: one pending request for the whole page.
    fake: (req) => fakeOutcome(shared, me, req, now()),
    // The requester polls file 6 alone; a dead link is its to report (it stops polling and the send rejects).
    getMe: async () => { const next = await fetchMe(); if (!disposed) takeMe(next); return next; },
  });
  requester.onChange(() => { if (!disposed) render(); });

  const dispose = () => {
    disposed = true;
    requester.stop();
    for (const undo of cleanups.splice(0)) undo();
  };
  const fail = (text) => { dispose(); notice(text); };

  // ---- static frame ----
  const head = el("header");
  const filters = el("section", { class: "filters" });
  const heatBox = el("div");
  const tip = el("div", { class: "tip", "aria-live": "polite" });
  const legend = el("div", { class: "legend" });
  const rail = el("aside", { class: "rail" });
  const status = el("div", { class: "status", role: "status" });
  const tabBar = el("div", { class: "viewtabs", role: "group", "aria-label": "View" });
  const leaguePanel = el("div", {}, head, filters,
    el("section", { class: "work" }, el("div", { class: "workmain" }, heatBox, tip, legend), rail));
  const laterPanel = el("div");
  let tab = defaultTab(shared, me); // remembered in memory only
  const tabButton = (id, text) => {
    const b = el("button", { type: "button", text });
    b.addEventListener("click", () => { tab = id; render(); });
    return [id, b];
  };
  const tabButtons = [tabButton("league", "League match"), tabButton("later", "Later games")];
  tabBar.append(...tabButtons.map(([, b]) => b));
  root.classList.add("wide");
  root.replaceChildren(tabBar, leaguePanel, laterPanel);

  // ---- heatmap ----
  const draft = () => (shared.booking && shared.booking.draft) || null;
  // Booked by file 5's booking, its match status, or our own confirmed request.
  const isBooked = () => draftBooked(shared);
  const bookedStart = () => (draft() ? cellOfIso(shared, draft().start_utc) : null);
  const inDraft = (j) => { const b = bookedStart(); return b !== null && j >= b && j < b + shared.rules.draft_cells; };
  const totalSubs = (subs) => subs[0] + subs[1];

  // A cell judged as a draft starting there: its classes and the words for its label.
  const cellState = (j) => {
    if (instant(j) < now()) return { cls: "past", words: "past" };
    const n = freeCount(shared, j, off);
    const some = n[0] + n[1] > 0 ? "short" : "empty";
    if (isBooked()) return inDraft(j) ? { cls: some, words: "the booked draft" } : { cls: some, words: "" };
    const r = draftCheck(shared, j, off, now());
    if (r.ok) {
      const t = totalSubs(r.subs);
      return { cls: `bookable s${Math.min(2, t)}`, words: `bookable, ${t ? plural(t, "sub") : "no subs"}` };
    }
    if (r.reason === "no_library") return { cls: `no-library s${Math.min(2, totalSubs(r.subs))}`, words: "no library copy free" };
    return { cls: some, words: "not bookable" };
  };
  // Each cell's look and words, and the rail's offers, are worked out once per state (file 5, the filters, the
  // booking, the current half hour) and reused by every callback; tapping a cell only moves the picked mark.
  let memoKey = null;
  const memo = new Map();
  let bestMemo = null, missMemo = null;
  const syncMemo = () => {
    const key = [shared.seq, [...off].sort().join(","), draft() ? draft().start_utc : "", shared.match.status,
      Math.floor((now() - startUtc) / CELL_MS)].join("|");
    if (key !== memoKey) { memoKey = key; memo.clear(); bestMemo = null; missMemo = null; }
  };
  const cellView = (j) => {
    let v = memo.get(j);
    if (!v) {
      const { cls, words } = cellState(j);
      const n = freeCount(shared, j, off);
      v = {
        cls: cls + (inDraft(j) ? " draft" : ""),
        label: `${timeLabel(instant(j), zone)}: ${shared.teams[0].name} ${n[0]} · ${shared.teams[1].name} ${n[1]} free${words ? `, ${words}` : ""}`,
        text: cls !== "past" && n[0] + n[1] > 0 ? n.join("·") : "",
      };
      memo.set(j, v);
    }
    return v;
  };
  const bestNow = () => (bestMemo ??= bestTimes(shared, off, now(), zone));
  const missesNow = () => (missMemo ??= closestMisses(shared, off, now(), zone));
  syncMemo();
  // The cell a narrow screen opens on: the booked draft, else the best time, else the first future cell.
  function firstUseful() {
    if (isBooked()) return bookedStart() ?? futureCell();
    const best = bestNow()[0];
    return best ? best.start : futureCell();
  }
  function futureCell() { return Math.max(0, Math.ceil((now() - startUtc) / CELL_MS)); }
  const heat = mountHeatmap(heatBox, {
    startUtc, zone,
    cellClass: (j) => cellView(j).cls + (j === picked ? " picked" : ""),
    cellLabel: (j) => cellView(j).label,
    cellText: (j) => cellView(j).text,
    onPick: (j) => { picked = j; render(); },
    initialJ: firstUseful(),
  });

  // ---- actions ----
  const book = (start) => {
    sendError = null;
    copied = false;
    const req = draftRequest(shared, start, now(), crypto.randomUUID());
    requester.send(req).then((o) => {
      if (disposed) return;
      if (o.status === "booked" && !draft()) {
        // Show the booking at once; the bot's next file 5 says the same.
        shared.booking = { ...(shared.booking || { sittings: [] }), draft: {
          start_utc: req.start_utc, by: viewer, booked_at: isoSeconds(now()),
          library_copy: copyNeeded(shared).needed,
        } };
      }
      if (o.status === "booked" && bookedStart() !== null) heat.showCell(bookedStart());
      render();
      // A refusal may mean file 5 moved on (someone booked first): fetch it now rather than in a minute.
      if (o.status === "refused") refresh();
    }, (e) => {
      if (disposed) return;
      if (e instanceof LinkExpired) return fail(MATCH_EXPIRED);
      console.error(e);
      sendError = sendErrorText(e);
      render();
    });
  };
  const bookButton = (start, text = "Book") => {
    const s = requester.state();
    const mine = s.pending && s.request && s.request.kind === "book_draft" && Date.parse(s.request.start_utc) === instant(start).getTime();
    const b = el("button", { type: "button", class: "act", text: mine ? "Booking…" : text });
    b.disabled = s.pending;
    b.addEventListener("click", () => book(start));
    return b;
  };

  const copyText = (text) => {
    copyBox = null;
    copied = false;
    let p;
    try { p = navigator.clipboard.writeText(text); } catch (e) { p = Promise.reject(e); }
    p.then(() => { copied = true; render(); }, () => { copyBox = text; copyBoxNew = true; render(); });
  };
  const missLine = (m) => {
    if (m.reason === "no_library") return `Players are free, but no library copy is free for ${andList(m.who)}'s games after it`;
    const free = freeFor(m.start);
    return shared.teams.map((t, ti) => (free[ti].length >= shared.rules.min_own_players ? "" :
      `${t.name} has ${free[ti].length ? "only " + andList(free[ti]) : "nobody"}`)).filter(Boolean).join("; ");
  };
  const matchName = () => `${shared.teams[0].name} vs ${shared.teams[1].name}`;
  const missText = (misses) => misses.map((m) => `- ${discordTime(instant(m.start))}: ${missLine(m)}`).join("\n");
  const nudgeText = (misses) => {
    const names = andList(otherTeam.players.map((p) => p.name));
    return `${names}: could you add more evenings to your usual week (My usual week in /settings)? ` +
      `We can't find a draft time for ${matchName()} with enough of us free.` +
      (misses.length ? `\nClosest misses:\n${missText(misses)}` : "");
  };
  const adminText = (misses) =>
    `League admins: ${matchName()} (${shared.match.tournament_name}, round ${shared.match.round}) can't find a draft time this week.` +
    (misses.length ? `\nClosest misses:\n${missText(misses)}` : "");

  // ---- drawing ----
  const renderHead = () => {
    const kids = [
      el("p", { class: "eyebrow", text: `${shared.match.tournament_name} · Round ${shared.match.round} · times in your zone (${zoneName})` }),
      el("h1", {}, shared.teams[0].name, el("span", { class: "vs", text: " vs " }), shared.teams[1].name),
      el("p", { class: "lede", text: isBooked() ? "The draft is booked. Tap a cell to see who's free then; tap a player to leave them out of the count." : `Pick the draft time. A time is bookable when each team has at least ${shared.rules.min_own_players} of its own players free for the whole draft (${duration(shared.rules.draft_cells)}); subs fill the rest. Tap a player to leave them out of the count.` }),
    ];
    const lib = libraryLine(shared);
    if (lib) kids.push(el("p", { class: "libline", text: lib }));
    if (typeof shared.match.thread_url === "string" && shared.match.thread_url.startsWith("https://")) {
      kids.push(el("p", { class: "threadlink" }, el("a", { href: shared.match.thread_url, target: "_blank", rel: "noopener", text: "Open the match thread" })));
    }
    head.replaceChildren(...kids);
  };

  const renderFilters = () => {
    const hl = picked === null ? new Set() : new Set(freeFor(picked).flat());
    const teamBlock = (t, ti) => el("div", { class: "team" },
      el("div", { class: "team-name" }, el("i", { class: `dot t${ti}` }), t.name),
      el("div", { class: "chips" }, ...t.players.map((p) => {
        const on = !off.has(p.user);
        const label = p.name + (p.user === viewer ? " (you)" : "");
        const b = el("button", { type: "button", class: `chip t${ti} ${on ? "on" : "off"}${on && hl.has(p.name) ? " hl" : ""}`, "aria-pressed": String(on) }, label);
        if (p.source === "none") b.append(el("span", { class: "subtag", text: " sub?" }));
        b.addEventListener("click", () => { if (off.has(p.user)) off.delete(p.user); else off.add(p.user); render(); });
        return b;
      })));
    const everyone = el("button", { type: "button", class: "linkbtn", text: "Everyone" });
    everyone.addEventListener("click", () => { off.clear(); render(); });
    const justMe = el("button", { type: "button", class: "linkbtn", text: "Just me" });
    justMe.addEventListener("click", () => { off.clear(); for (const p of players()) if (p.user !== viewer) off.add(p.user); render(); });
    filters.replaceChildren(el("div", { class: "teams" }, ...shared.teams.map(teamBlock), el("div", { class: "quick" }, everyone, justMe)));
  };

  const bookedLabel = () => (draft() ? timeLabel(new Date(draft().start_utc), zone) : null);
  const checkSentence = (j) => {
    if (isBooked()) return bookedLabel() ? `The draft is booked for ${bookedLabel()}.` : "The draft is booked.";
    if (j + shared.rules.draft_cells > shared.window.cells) return "A draft starting here would run past the end of this match's week.";
    const r = draftCheck(shared, j, off, now());
    if (r.reason === "past") return "That time has passed.";
    if (r.reason === "no_library") {
      return `Enough players are free, but no league library copy is free for the games after this draft, which ${andList(copyNeeded(shared).who)} need. DraftBot would refuse it.`;
    }
    if (r.reason === "not_bookable") return `Not bookable: each team needs ${shared.rules.min_own_players} of its own players free for the whole draft.`;
    const subs = shared.teams.map((t, ti) => (r.subs[ti] ? `${t.name} needs ${plural(r.subs[ti], "sub")}` : "")).filter(Boolean);
    return `Bookable${subs.length ? `: ${subs.join(", ")}` : " with no subs"}.`;
  };

  const renderTip = () => {
    if (picked === null) { tip.replaceChildren("Tap a cell to check a draft starting then."); return; }
    const free = freeFor(picked);
    const end = instant(picked + shared.rules.draft_cells);
    const kids = [
      el("b", { text: timeLabel(instant(picked), zone) }), ` to ${formatClock(end, zone)}`, el("br"),
      ...shared.teams.flatMap((t, ti) => [ti ? " · " : "", el("span", { class: `tn t${ti}`, text: `${t.name}:` }), ` ${free[ti].join(", ") || "nobody"}`]),
      el("br"), checkSentence(picked),
    ];
    if (!isBooked() && draftCheck(shared, picked, off, now()).ok) kids.push(" ", bookButton(picked, "Book this time"));
    tip.replaceChildren(...kids);
  };

  const renderLegend = () => {
    const sw = (cls, text) => el("span", {}, el("i", { class: `sw ${cls}` }), text);
    const count = `numbers read ${shared.teams[0].name}·${shared.teams[1].name} free`;
    if (isBooked()) {
      const items = draft() ? [sw("draftsw", "the booked draft")] : [];
      legend.replaceChildren(...items, sw("pastsw", "past"), el("span", { text: `The draft is booked; ${count}` }));
      return;
    }
    const items = [sw("s0", "no subs"), sw("s1", "1 sub"), sw("s2", "2 subs"), sw("", "not bookable")];
    if (copyNeeded(shared).needed) items.push(sw("nolib", "no library copy free"));
    items.push(sw("pastsw", "past"));
    items.push(el("span", { text: `A draft starts in the cell; ${count}` }));
    legend.replaceChildren(...items);
  };

  const renderRail = () => {
    const s = requester.state();
    const out = players().filter((p) => off.has(p.user)).map((p) => p.name);
    const kids = [];
    if (isBooked()) {
      kids.push(el("h2", { text: "Booked" }), el("div", { class: "booked", role: "status" },
        el("b", { text: "Draft booked" }), bookedLabel() ? `: ${bookedLabel()}. Posted in the match thread.` : ". See the match thread."));
      if (pointsToLater(shared, me)) {
        const go = el("button", { type: "button", class: "act", text: "Book your games under Later games" });
        go.addEventListener("click", () => { tab = "later"; render(); });
        kids.push(go);
      }
    } else {
      kids.push(el("h2", { text: "Best times" }), el("div", { class: "who", text: out.length ? `Counting without ${andList(out)}` : "Counting everyone" }));
      const best = bestNow();
      if (best.length) {
        kids.push(el("div", { class: "stack" }, ...best.map((b) => {
          const subText = shared.teams.map((t, ti) => (b.teamSubs[ti] ? `${t.name} ${plural(b.teamSubs[ti], "sub")}` : "")).filter(Boolean).join(", ") || "no subs";
          return el("div", { class: "slot" }, el("span", {}, el("span", { class: "when", text: b.label }), el("br"), el("span", { class: "subs", text: subText })), bookButton(b.start));
        })));
      } else {
        const misses = missesNow();
        kids.push(el("div", { class: "alert" }, el("b", { text: "No time works this week." }),
          ` No hour has ${shared.rules.min_own_players} players from each team free${misses.length ? ". Closest misses:" : "."}`));
        if (misses.length) {
          kids.push(el("div", { class: "stack" }, ...misses.map((m) => el("div", { class: "slot miss" },
            el("span", {}, el("span", { class: "when", text: m.label }), el("br"), el("span", { class: "subs", text: missLine(m) }))))));
        }
        const nudge = el("button", { type: "button", class: "act ghost", text: `Ask ${otherTeam.name} to add times` });
        nudge.addEventListener("click", () => copyText(nudgeText(misses)));
        const admin = el("button", { type: "button", class: "act ghost", text: "Tell the league admins" });
        admin.addEventListener("click", () => copyText(adminText(misses)));
        kids.push(el("div", { class: "stack" }, nudge, admin));
        if (copyBox) {
          const box = el("textarea", { class: "copybox", readonly: "", rows: "5", "aria-label": "Message to copy" });
          box.value = copyBox;
          kids.push(el("p", { class: "status", text: "Copy this and paste it in the match thread:" }), box);
          if (copyBoxNew) {
            copyBoxNew = false;
            queueMicrotask(() => { box.focus(); box.select(); });
          }
        }
      }
    }
    // Status: the pending request, the bot's refusal, or a send error.
    const st = requestStatus(s, "book_draft", REFUSALS, sendError);
    status.className = st && st.warn ? "status warn" : "status";
    status.textContent = st ? st.text : copied ? "Copied — paste it in the match thread" : "";
    kids.push(status);
    rail.replaceChildren(...kids);
  };

  // ---- later games ----
  const later = mountLater(laterPanel, {
    getShared: () => shared, setShared: (next) => { shared = next; }, getMe: () => me,
    viewer, zone, now, requester, render: () => render(), fail, isDisposed: () => disposed, expiredText: MATCH_EXPIRED,
  });

  const renderTabs = () => {
    const shown = laterTabShown(shared, me);
    const active = shown ? tab : "league";
    tabBar.hidden = !shown;
    for (const [id, b] of tabButtons) b.setAttribute("aria-pressed", String(id === active));
    leaguePanel.hidden = active !== "league";
    laterPanel.hidden = active !== "later";
    return active;
  };

  function render() {
    syncMemo();
    if (renderTabs() === "later") { later.render(); return; }
    renderHead();
    renderFilters();
    renderTip();
    renderLegend();
    renderRail();
    heat.update();
  }

  // ---- live updates (real links only): files 5 and 6 every 60 s and when the tab comes back into view, one
  // fetch at a time; while a request waits, the requester polls file 6 alone every 5 s ----
  // File 6 is rewritten on its own (a deck check, a request's answer): redraw when either file moves on.
  function takeMe(next) {
    const changed = next.seq !== me.seq;
    me = next;
    if (changed) render();
  }
  let inflight = null;
  function refresh() {
    if (link.fixture || disposed) return Promise.resolve();
    inflight ??= (async () => {
      try {
        const next = await fetchBoth();
        if (disposed) return;
        const sharedChanged = next.shared.seq > shared.seq;
        if (sharedChanged) shared = next.shared;
        const meChanged = next.me.seq !== me.seq;
        me = next.me;
        if (sharedChanged || meChanged) render();
      } catch (e) {
        if (e instanceof LinkExpired) return fail(MATCH_EXPIRED);
        console.warn("refreshing the match failed", e);
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }
  if (!link.fixture) {
    const id = setInterval(() => { if (!requester.state().pending) refresh(); }, REFRESH_MS);
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    cleanups.push(() => clearInterval(id), () => document.removeEventListener("visibilitychange", onVisible));
  }

  render();
  return { dispose };
}
