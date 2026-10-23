// The league match view: who's free over the match week, the best draft hours, and the Book request.
// The model lives in match.js and the request flow in request.js; this file only draws and wires them.
import {
  parseShared, parseMe, copyNeeded, freeCount, draftCheck, bestTimes, closestMisses, timeLabel,
  draftRequest, fakeOutcome, REFUSALS,
} from "./match.js";
import { CELL_MINUTES } from "./week.js";
import { mountHeatmap } from "./grid.js";
import { createRequester, SLOW_MESSAGE } from "./request.js";
import { LinkExpired } from "./api.js";
import { expiredMessage } from "./messages.js";
import { el } from "./dom.js";

const MATCH_EXPIRED = expiredMessage("match");
const NEEDS_UPDATE = "This page needs updating.";
const REFRESH_MS = 60000;
const CELL_MS = CELL_MINUTES * 60000;

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);
const duration = (cells) => {
  const min = cells * CELL_MINUTES;
  return min % 60 === 0 ? plural(min / 60, "hour") : `${min} minutes`;
};
const zoneAbbrev = (date, zone) =>
  new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(date)
    .find((p) => p.type === "timeZoneName")?.value || zone;
const discordTime = (date) => `<t:${Math.floor(date.getTime() / 1000)}:f>`;

// The one sentence under the header about what follows the draft, or null when there is nothing to say.
// Nobody following is said whether or not the server has a library; the copy sentences need one.
export function libraryLine(shared) {
  const follows = shared.teams.some((t) => t.players.some((p) => p.plays_after_draft === true));
  if (!follows) return "Nobody plays right after the draft. Once it's booked, each of you books your games under Later games.";
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

  const fetchBoth = async () => {
    const [s, m] = await Promise.all([deps.getJson(link.urls.shared_get), deps.getJson(link.urls.me_get)]);
    return { shared: parseShared(s), me: parseMe(m) };
  };

  root.replaceChildren(el("p", { class: "status", text: "Loading the match…" }));
  let shared, me;
  try {
    ({ shared, me } = await fetchBoth());
  } catch (e) {
    if (e instanceof LinkExpired) { notice(MATCH_EXPIRED); return noop; }
    if (e && e.message === NEEDS_UPDATE) { notice(NEEDS_UPDATE); return noop; }
    throw e;
  }

  // Fixture walkthroughs run at the moment the files were made, so every scenario reads the same.
  const fixtureNow = new Date(shared.generated_at);
  const now = link.fixture ? () => fixtureNow : (deps.now || (() => new Date()));
  const startUtc = new Date(shared.window.start_utc);
  const instant = (j) => new Date(startUtc.getTime() + j * CELL_MS);
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
    !off.has(p.user) && [...Array(shared.rules.draft_cells).keys()].every((k) => p.cells.has(j + k))).map((p) => p.name));

  const requester = createRequester({
    putUrl: link.urls.request_put,
    put: deps.putJson,
    fixture: link.fixture,
    fake: (req) => fakeOutcome(shared, req, now()),
    getMe: async () => { await refresh(true); return me; },
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
  root.classList.add("wide");
  root.replaceChildren(head, filters,
    el("section", { class: "work" }, el("div", { class: "workmain" }, heatBox, tip, legend), rail));

  // ---- heatmap ----
  const draft = () => (shared.booking && shared.booking.draft) || null;
  // Booked by file 5's booking, its match status, or our own confirmed request.
  const isBooked = () => Boolean(draft()) || shared.match.status === "booked";
  const bookedStart = () => (draft() ? (new Date(draft().start_utc) - startUtc) / CELL_MS : null);
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
  const cellClass = (j) => cellState(j).cls + (inDraft(j) ? " draft" : "") + (j === picked ? " picked" : "");
  const countText = (j) => freeCount(shared, j, off).join("·");
  const cellLabel = (j) => {
    const n = freeCount(shared, j, off);
    const words = cellState(j).words;
    return `${timeLabel(instant(j), zone)}: ${shared.teams[0].name} ${n[0]} · ${shared.teams[1].name} ${n[1]} free${words ? `, ${words}` : ""}`;
  };
  const cellText = (j) => {
    if (instant(j) < now()) return "";
    const n = freeCount(shared, j, off);
    return n[0] + n[1] > 0 ? countText(j) : "";
  };
  const heat = mountHeatmap(heatBox, {
    startUtc, zone, cellClass, cellLabel, cellText,
    onPick: (j) => { picked = j; render(); },
  });

  // ---- actions ----
  const book = (start) => {
    sendError = null;
    copied = false;
    const req = draftRequest(shared, start, now(), crypto.randomUUID());
    requester.send(req).then((o) => {
      if (o.status === "booked" && !draft()) {
        // Show the booking at once; the bot's next file 5 says the same.
        shared.booking = { ...(shared.booking || { sittings: [] }), draft: {
          start_utc: req.start_utc, by: viewer, booked_at: now().toISOString().replace(/\.\d{3}Z$/, "Z"),
          library_copy: copyNeeded(shared).needed,
        } };
      }
      render();
    }, (e) => {
      if (e instanceof LinkExpired) return fail(MATCH_EXPIRED);
      console.error(e);
      sendError = /pending/.test(e.message) ? "One request is already waiting for DraftBot." : "Couldn't send the request. Try again.";
      render();
    });
  };
  const bookButton = (start, text = "Book") => {
    const s = requester.state();
    const mine = s.pending && s.request && Date.parse(s.request.start_utc) === instant(start).getTime();
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
      el("p", { class: "eyebrow", text: `${shared.match.tournament_name} · Round ${shared.match.round} · times in your zone (${zoneAbbrev(startUtc, zone)})` }),
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
      el("b", { text: timeLabel(instant(picked), zone) }), ` to ${timeLabel(end, zone).split(", ")[1]}`, el("br"),
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
    } else {
      kids.push(el("h2", { text: "Best times" }), el("div", { class: "who", text: out.length ? `Counting without ${andList(out)}` : "Counting everyone" }));
      const best = bestTimes(shared, off, now(), zone);
      if (best.length) {
        kids.push(el("div", { class: "stack" }, ...best.map((b) => {
          const subs = draftCheck(shared, b.start, off, now()).subs;
          const subText = shared.teams.map((t, ti) => (subs[ti] ? `${t.name} ${plural(subs[ti], "sub")}` : "")).filter(Boolean).join(", ") || "no subs";
          return el("div", { class: "slot" }, el("span", {}, el("span", { class: "when", text: b.label }), el("br"), el("span", { class: "subs", text: subText })), bookButton(b.start));
        })));
      } else {
        const misses = closestMisses(shared, off, now(), zone);
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
    status.className = "status";
    if (s.pending) status.textContent = s.slow ? SLOW_MESSAGE : "Booking… DraftBot confirms in a few seconds";
    else if (sendError) { status.textContent = sendError; status.className = "status warn"; }
    else if (s.outcome && s.outcome.status === "refused") {
      status.textContent = REFUSALS[s.outcome.reason] || "DraftBot refused that request.";
      status.className = "status warn";
    } else status.textContent = copied ? "Copied — paste it in the match thread" : "";
    kids.push(status);
    rail.replaceChildren(...kids);
  };

  function render() {
    renderHead();
    renderFilters();
    renderTip();
    renderLegend();
    renderRail();
    heat.update();
  }

  // ---- live updates (real links only): files 5 and 6 every 60 s and on focus; the requester polls at 5 s ----
  // fromRequester: a dead link is the requester's to report (it stops polling and the send rejects).
  async function refresh(fromRequester = false) {
    if (link.fixture || disposed) return;
    try {
      const next = await fetchBoth();
      if (disposed) return;
      // File 6 is rewritten on its own (a deck check, a request's answer): redraw for either file.
      const meChanged = next.me.seq !== me.seq;
      me = next.me;
      const sharedChanged = next.shared.seq > shared.seq;
      if (sharedChanged) shared = next.shared;
      if (sharedChanged || meChanged) render();
    } catch (e) {
      if (e instanceof LinkExpired) {
        if (fromRequester) throw e;
        return fail(MATCH_EXPIRED);
      }
      console.warn("refreshing the match failed", e);
    }
  }
  if (!link.fixture) {
    const id = setInterval(() => { if (!requester.state().pending) refresh(); }, REFRESH_MS);
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    const onFocus = () => refresh();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    cleanups.push(() => clearInterval(id),
      () => document.removeEventListener("visibilitychange", onVisible),
      () => window.removeEventListener("focus", onFocus));
  }

  render();
  return { dispose };
}
