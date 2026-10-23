// Find a team: compare the viewer's usual week with each live card. Pure model (fitCard, rankCards) plus a thin view.
import { CELL_MINUTES, CELL_MS, decodeCells, windowCells } from "./week.js";
import { loadWeek, mountUsual } from "./usual.js";
import { expiredMessage, loadFailedMessage } from "./messages.js";
import { mountHeatmap, formatCell } from "./grid.js";
import { LinkExpired, checkDoc } from "./api.js";
import { el } from "./dom.js";
import { plural, pluralWord } from "./words.js";

const FORMAT = "draftbot.scheduler.lfg";

// viewer: { zone, cells } (usual week). Cells where the viewer and every member are free in the window.
export function fitCard(viewer, card, startUtc) {
  const you = windowCells(viewer.zone, viewer.cells, startUtc);
  const perMember = new Map();
  let overlap = new Set(you);
  for (const m of card.members) {
    const theirs = windowCells(m.zone, decodeCells(m.cells), startUtc);
    perMember.set(m.user, theirs);
    overlap = new Set([...overlap].filter((j) => theirs.has(j)));
  }
  return { overlap, hours: (overlap.size * CELL_MINUTES) / 60, perMember, you };
}

const idOrder = (a, b) => a.localeCompare(b, "en", { numeric: true });

// [{ card, fit }], best fit first, ties by card_id.
export function rankCards(viewer, cards, startUtc) {
  return cards
    .map((card) => ({ card, fit: fitCard(viewer, card, startUtc) }))
    .sort((a, b) => b.fit.hours - a.fit.hours || idOrder(a.card.card_id, b.card.card_id));
}

export function halfHourFloor(date) {
  return new Date(Math.floor(date.getTime() / CELL_MS) * CELL_MS);
}

const names = (card) => card.members.map((m) => m.name).join(" & ");

async function loadLfg(url, getJson) {
  return checkDoc(await getJson(url), FORMAT).cards || [];
}

// deps: { getJson, putJson, now?: () => Date }
export async function mountLfg(root, link, deps) {
  const now = deps.now || (() => new Date());
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const notice = (text) => root.replaceChildren(el("p", { class: "notice", text }));

  const edit = () => {
    mountUsual(root, link, deps).then(() => {
      const back = el("button", { type: "button", class: "secondary back", text: "Back" });
      back.addEventListener("click", () => {
        mountLfg(root, link, deps).catch((e) => { console.error(e); notice(loadFailedMessage(e)); });
      });
      root.prepend(back);
    }).catch((e) => { console.error(e); notice(loadFailedMessage(e)); });
  };
  const editButton = () => {
    const b = el("button", { type: "button", class: "secondary", text: "Edit my usual week" });
    b.addEventListener("click", edit);
    return b;
  };

  root.replaceChildren(el("p", { class: "status", text: "Loading…" }));
  let cards, week;
  try {
    [cards, week] = await Promise.all([loadLfg(link.urls.lfg_get, deps.getJson), loadWeek(link.urls.usual_get, deps.getJson)]);
  } catch (e) {
    if (e instanceof LinkExpired) return notice(expiredMessage("lfg"));
    console.error(e);
    return notice(loadFailedMessage(e));
  }

  const head = [el("p", { class: "eyebrow", text: "#find-a-team" }), el("h1", { text: "How well do we fit?" })];
  if (week.cells.size === 0) {
    root.replaceChildren(...head, el("p", { class: "lede", text: "Paint your usual week first, then come back to see how it lines up with each card." }), el("div", { class: "actions" }, editButton()));
    return;
  }
  if (cards.length === 0) {
    root.replaceChildren(...head, el("p", { class: "lede", text: "Nobody is looking right now." }), el("div", { class: "actions" }, editButton()));
    return;
  }

  const zone = week.zone || browserZone;
  const viewer = { zone, cells: week.cells };
  const startUtc = halfHourFloor(now());
  const ranked = rankCards(viewer, cards, startUtc);
  let selected = ranked[0].card.card_id;
  const fits = new Map(ranked.map((r) => [r.card.card_id, r]));

  const list = el("div", { class: "posts" });
  const summary = el("div", { class: "summary" });
  const heatBox = el("div");
  let heat = null;

  const tint = (j) => {
    const { card, fit } = fits.get(selected);
    const total = card.members.length + 1;
    let n = fit.you.has(j) ? 1 : 0;
    for (const set of fit.perMember.values()) if (set.has(j)) n++;
    return { n, total };
  };
  const cellClass = (j) => {
    const { n, total } = tint(j);
    if (n === total) return "b0";
    if (n === total - 1 && total > 1 && n > 0) return "b1";
    return n > 0 ? "b2" : "";
  };
  const cellLabel = (j) => {
    const { n, total } = tint(j);
    return `${formatCell(startUtc, zone, j)}: ${n} of ${total} free`;
  };

  const renderSummary = () => {
    const { card, fit } = fits.get(selected);
    const kids = [el("p", { class: "total" }, el("span", { class: "big", text: String(fit.hours) }), ` ${pluralWord(fit.hours, "hour")} a week with ${names(card)}`)];
    const better = ranked.find((r) => r.card.card_id !== selected && r.fit.hours > fit.hours);
    if (better) {
      const jump = el("button", { type: "button", class: "secondary", text: `See ${names(better.card)} instead (${plural(better.fit.hours, "hour")})` });
      jump.addEventListener("click", () => select(better.card.card_id));
      kids.push(jump);
    }
    kids.push(el("p", { class: "status", text: "Reply on their card in #find-a-team." }));
    summary.replaceChildren(...kids);
  };

  const select = (id) => {
    selected = id;
    for (const b of list.children) b.setAttribute("aria-pressed", String(b.dataset.id === id));
    renderSummary();
    heat.update();
  };

  for (const { card, fit } of ranked) {
    const b = el("button", { type: "button", class: "post", "data-id": card.card_id, "aria-pressed": String(card.card_id === selected) },
      el("span", { class: "pn", text: names(card) }),
      el("span", { class: "pm", text: card.note }),
      el("span", { class: "fit", text: `${plural(fit.hours, "hour")} this week` }));
    b.addEventListener("click", () => select(card.card_id));
    list.append(b);
  }

  const legend = el("div", { class: "legend" },
    el("span", {}, el("i", { class: "sw b0" }), "all of you"),
    el("span", {}, el("i", { class: "sw b1" }), "all but one"),
    el("span", {}, el("i", { class: "sw b2" }), "some"),
    el("span", {}, el("i", { class: "sw" }), "nobody"));

  root.replaceChildren(
    ...head,
    el("p", { class: "lede", text: `Your usual week against each card, over the next 7 days. Times in ${zone.replaceAll("_", " ")}. Darker means more of you are free.` }),
    list, summary, heatBox, legend,
    el("div", { class: "actions" }, editButton()),
  );
  heat = mountHeatmap(heatBox, { startUtc, zone, cellClass, cellLabel,
    initialJ: Math.max(0, Math.ceil((now() - startUtc) / (30 * 60000))) });
  renderSummary();
}
