// The later-games view: for each pairing, when you and your opponent are both free after the draft, and
// the Book request for that game (reserving your library deck or not). The model lives in later.js; the
// match view owns the tab bar and the one requester the draft and the games share.
import { timeLabel } from "./match.js";
import {
  myOpponents, deckOk, earliest, sittingStarts, sittingRequest, fakeSittingOutcome, SITTING_REFUSALS,
} from "./later.js";
import { CELL_MINUTES } from "./week.js";
import { mountHeatmap } from "./grid.js";
import { SLOW_MESSAGE } from "./request.js";
import { LinkExpired } from "./api.js";
import { el } from "./dom.js";

const CELL_MS = CELL_MINUTES * 60000;
const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

export const draftBooked = (shared) => Boolean(shared.booking && shared.booking.draft) || shared.match.status === "booked";
const nobodyFollows = (shared) => !shared.teams.some((t) => t.players.some((p) => p.plays_after_draft === true));

// The Later games tab: the viewer has a pairing, and the draft is booked or nobody plays right after it.
export function laterTabShown(shared, me) {
  return myOpponents(shared, me).length > 0 && (draftBooked(shared) || nobodyFollows(shared));
}

// Later games first only for a viewer who doesn't play right after an already booked draft.
export function defaultTab(shared, me) {
  return laterTabShown(shared, me) && draftBooked(shared) && me.plays_after_draft === false ? "later" : "league";
}

// Whether this viewer's games involve their library deck at all.
export function usesLibrary(shared, me) {
  return Boolean(shared.library && shared.library.enabled && me.needs_library && me.deck && me.deck.assigned);
}

// The buttons for one offered start: [{ text, reserve }].
export function sittingButtons(me, start, { library, moving = false }) {
  const verb = moving ? "Move" : "Book";
  if (!library) return [{ text: moving ? "Move here" : "Book", reserve: false }];
  const out = [];
  if (deckOk(me, start)) out.push({ text: `${verb} + reserve`, reserve: true });
  out.push({ text: `${verb} without library`, reserve: false });
  return out;
}

export function bookedLine(sitting, viewer, zone) {
  const deck = (sitting.decks_reserved || []).includes(viewer) ? "your deck is reserved" : "no deck reserved";
  return `Booked: ${timeLabel(new Date(sitting.start_utc), zone)}, ${deck}.`;
}

// shared with the request's sitting in place of any earlier one for the same pairing (a booking moves it).
export function applySitting(shared, me, request) {
  const players = [me.user, request.opponent];
  const booking = shared.booking || { draft: null, sittings: [] };
  const sittings = (booking.sittings || []).filter((s) => !sameSet(s.players, players));
  sittings.push({
    players, start_utc: request.start_utc, cells: shared.rules.sitting_cells, by: me.user,
    decks_reserved: request.reserve_deck ? [me.user] : [],
  });
  return { ...shared, booking: { ...booking, sittings } };
}

const gameLength = (cells) => {
  const h = (cells * CELL_MINUTES) / 60;
  if (Number.isInteger(h)) return `${h} hour${h === 1 ? "" : "s"}`;
  return h % 1 === 0.5 ? `${Math.floor(h) || ""}½ hours` : `${cells * CELL_MINUTES} minutes`;
};

// ctx: { getShared(), setShared(next), getMe(), viewer, zone, now(), requester, render() (the whole page),
//        fail(text), isDisposed(), expiredText }
export function mountLater(box, ctx) {
  const { viewer, zone, now, requester } = ctx;
  let opp = null;
  let picked = null;
  let sendError = null;

  const shared = () => ctx.getShared();
  const me = () => ctx.getMe();
  const startUtc = () => new Date(shared().window.start_utc);
  const instant = (j) => new Date(startUtc().getTime() + j * CELL_MS);
  const cellOfIso = (iso) => (new Date(iso) - startUtc()) / CELL_MS;
  const library = () => usesLibrary(shared(), me());
  const player = (user) => shared().teams.flatMap((t) => t.players).find((p) => p.user === user);
  const opponents = () => myOpponents(shared(), me());
  const current = () => {
    const os = opponents();
    return os.find((o) => o.user === opp) || os[0] || null;
  };
  const sittingCells = () => shared().rules.sitting_cells;
  const inBooked = (j) => {
    const o = current();
    if (!o || !o.booked) return false;
    const b = cellOfIso(o.booked.start_utc);
    return j >= b && j < b + (o.booked.cells || sittingCells());
  };

  // ---- frame ----
  const head = el("header");
  const posts = el("section", { class: "filters" });
  const banner = el("div");
  const heatBox = el("div");
  const tip = el("div", { class: "tip", "aria-live": "polite" });
  const legend = el("div", { class: "legend" });
  const rail = el("aside", { class: "rail" });
  const status = el("div", { class: "status", role: "status" });
  box.replaceChildren(head, posts, banner,
    el("section", { class: "work" }, el("div", { class: "workmain" }, heatBox, tip, legend), rail));

  // ---- heatmap: you and the selected opponent ----
  const cellState = (j) => {
    const o = current();
    if (!o) return { cls: "", words: "" };
    if (j < earliest(shared(), now())) return { cls: "past", words: draftBooked(shared()) ? "past or before the draft ends" : "past" };
    const a = player(viewer), b = player(o.user);
    const mine = Boolean(a && a.cells.has(j)), theirs = Boolean(b && b.cells.has(j));
    if (mine && theirs) {
      if (library() && !deckOk(me(), j)) return { cls: "s2 no-library", words: `you and ${o.name} free, your deck's cards are held` };
      return { cls: "s0", words: `you and ${o.name} free` };
    }
    if (mine || theirs) return { cls: "short", words: mine ? "only you free" : `only ${o.name} free` };
    return { cls: "", words: "neither of you free" };
  };
  const heat = mountHeatmap(heatBox, {
    startUtc: startUtc(), zone,
    cellClass: (j) => cellState(j).cls + (inBooked(j) ? " draft" : "") + (j === picked ? " picked" : ""),
    cellLabel: (j) => {
      const w = cellState(j).words;
      return `${timeLabel(instant(j), zone)}${w ? `: ${w}` : ""}${inBooked(j) ? ", your booked game" : ""}`;
    },
    onPick: (j) => { picked = j; ctx.render(); },
  });

  // ---- booking ----
  const book = (o, start, reserve) => {
    sendError = null;
    const req = sittingRequest(shared(), o.user, start, reserve, now(), crypto.randomUUID());
    requester.send(req).then((out) => {
      if (ctx.isDisposed()) return;
      // Show the game at once; the bot's next file 5 says the same.
      if (out.status === "booked") ctx.setShared(applySitting(shared(), me(), req));
      ctx.render();
    }, (e) => {
      if (ctx.isDisposed()) return;
      if (e instanceof LinkExpired) return ctx.fail(ctx.expiredText);
      console.error(e);
      sendError = /pending/.test(e.message) ? "One request is already waiting for DraftBot." : "Couldn't send the request. Try again.";
      ctx.render();
    });
  };
  const buttonsFor = (o, start) => {
    const s = requester.state();
    return sittingButtons(me(), start, { library: library(), moving: Boolean(o.booked) }).map(({ text, reserve }) => {
      const mine = s.pending && s.request && s.request.kind === "book_sitting" && s.request.opponent === o.user &&
        Date.parse(s.request.start_utc) === instant(start).getTime() && s.request.reserve_deck === reserve;
      const b = el("button", { type: "button", class: reserve || !library() ? "act" : "act ghost", text: mine ? "Booking…" : text });
      b.disabled = s.pending;
      b.addEventListener("click", () => book(o, start, reserve));
      return b;
    });
  };
  // Can a game vs o start at j? The same rules the bot's stand-in uses.
  const canStart = (o, j) => draftBooked(shared()) &&
    fakeSittingOutcome(shared(), me(), sittingRequest(shared(), o.user, j, false, now(), "check"), now()).status === "booked";
  const isBookedStart = (o, j) => Boolean(o.booked) && cellOfIso(o.booked.start_utc) === j;

  // ---- drawing ----
  const renderHead = () => {
    const sh = shared();
    const lede = [`Each of your games after the draft gets its own time, from your week and your opponent's. A game takes about ${gameLength(sittingCells())}.`];
    if (library()) lede.push(`Reserving your library deck holds its ${me().deck.cards} cards for that game.`);
    head.replaceChildren(
      el("p", { class: "eyebrow", text: `${sh.match.tournament_name} · Round ${sh.match.round} · later games` }),
      el("h1", { text: "Your games after the draft" }),
      el("p", { class: "lede", text: lede.join(" ") }));
  };

  const renderPosts = () => {
    const sel = current();
    posts.replaceChildren(el("div", { class: "posts" }, ...opponents().map((o) => {
      const b = el("button", { type: "button", class: "post", "aria-pressed": String(sel && sel.user === o.user) },
        el("span", { class: "pn", text: `vs ${o.name}` }),
        el("span", { class: "pm", text: o.booked ? `Booked: ${timeLabel(new Date(o.booked.start_utc), zone)}` : "Not booked yet" }));
      b.addEventListener("click", () => { opp = o.user; picked = null; sendError = null; ctx.render(); });
      return b;
    })));
  };

  const renderBanner = () => {
    const o = current();
    if (!o || !o.booked) { banner.replaceChildren(); return; }
    banner.replaceChildren(el("div", { class: "booked sitting", role: "status" },
      el("b", { text: `You vs ${o.name}` }), ` · ${bookedLine(o.booked, viewer, zone)} Booking another time moves it.`));
  };

  const renderTip = () => {
    const o = current();
    if (!o || picked === null) { tip.replaceChildren("Tap a cell to check a game starting then."); return; }
    const end = instant(picked + sittingCells());
    const kids = [el("b", { text: timeLabel(instant(picked), zone) }), ` to ${timeLabel(end, zone).split(", ")[1]}`, el("br")];
    if (isBookedStart(o, picked)) kids.push(`Your game vs ${o.name} is booked for this time.`);
    else if (!draftBooked(shared())) kids.push("Book the draft first: your games are played after it.");
    else if (picked < earliest(shared(), now())) kids.push("Games start after the draft ends, and never in the past.");
    else if (!canStart(o, picked)) kids.push(`You and ${o.name} aren't both free for the whole game.`);
    else {
      kids.push(library() && !deckOk(me(), picked) ? "You're both free, but some of your deck's cards are held then." : "You're both free for the whole game.");
      kids.push(" ", ...buttonsFor(o, picked));
    }
    tip.replaceChildren(...kids);
  };

  const renderLegend = () => {
    const o = current();
    const name = o ? o.name : "your opponent";
    const sw = (cls, text) => el("span", {}, el("i", { class: `sw ${cls}` }), text);
    const items = [sw("s0", `you and ${name} free`)];
    if (library()) items.push(sw("s2 nolib", "both free, your deck's cards held"));
    items.push(sw("", "not both free"));
    if (o && o.booked) items.push(sw("draftsw", "your booked game"));
    items.push(sw("pastsw", draftBooked(shared()) ? "past or before the draft ends" : "past"));
    legend.replaceChildren(...items);
  };

  const renderRail = () => {
    const o = current();
    const s = requester.state();
    const kids = [];
    if (!o) {
      kids.push(el("p", { class: "who", text: "DraftBot has no pairing for you in this match." }));
    } else {
      kids.push(el("h2", { text: `Times for you vs ${o.name}` }));
      if (library()) {
        kids.push(el("div", { class: "who", text: `Each time is checked for your deck's ${me().deck.cards} cards.` }));
        if (me().deck.short_reason) kids.push(el("div", { class: "who warn", text: me().deck.short_reason }));
      }
      if (!draftBooked(shared())) {
        kids.push(el("div", { class: "alert" }, el("b", { text: "Book the draft first." }), " Your games are played after it, so their times open once it's booked."));
      } else {
        const starts = sittingStarts(shared(), me(), o.user, now(), { reserve: false }, zone);
        if (starts.length) {
          kids.push(el("div", { class: "stack" }, ...starts.map((st) => {
            const here = isBookedStart(o, st.start);
            const sub = here ? "your game is booked here"
              : library() ? (st.deckOk ? `your deck is free · ${gameLength(sittingCells())}` : "some of your deck's cards are held then")
                : gameLength(sittingCells());
            const btns = here ? [] : buttonsFor(o, st.start);
            return el("div", { class: `slot later${library() && !st.deckOk && !here ? " miss" : ""}` },
              el("span", {}, el("span", { class: "when", text: (o.booked && !here ? "Move to " : "") + st.label }), el("br"), el("span", { class: "subs", text: sub })),
              el("span", { class: "btns" }, ...btns));
          })));
        } else {
          kids.push(el("div", { class: "alert" }, el("b", { text: "No shared time this week." }),
            ` You and ${o.name} aren't both free for a whole game after the draft. Ask ${o.name} to add times, or tell the league admins.`));
        }
      }
    }
    status.className = "status";
    if (s.pending) {
      status.textContent = s.slow ? SLOW_MESSAGE
        : s.request && s.request.kind === "book_sitting" ? "Booking… DraftBot confirms in a few seconds" : "Waiting for DraftBot to answer the draft booking…";
    } else if (sendError) { status.textContent = sendError; status.className = "status warn"; }
    else if (s.outcome && s.request && s.request.kind === "book_sitting" && s.outcome.status === "refused") {
      status.textContent = SITTING_REFUSALS[s.outcome.reason] || "DraftBot refused that request.";
      status.className = "status warn";
    } else if (s.outcome && s.request && s.request.kind === "book_sitting" && s.outcome.status === "booked" && o && s.request.opponent === o.user) {
      status.textContent = "Booked. DraftBot posts it in the match thread.";
    } else status.textContent = "";
    kids.push(status);
    rail.replaceChildren(...kids);
  };

  return {
    render() {
      renderHead();
      renderPosts();
      renderBanner();
      renderTip();
      renderLegend();
      renderRail();
      heat.update();
    },
  };
}
