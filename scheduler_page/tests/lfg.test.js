import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fitCard, rankCards } from "../js/lfg.js";
import { decodeCells, usualCellOf, CELLS } from "../js/week.js";

const fx = (n) => JSON.parse(readFileSync(new URL(`../fixtures/${n}`, import.meta.url)));
const lfg = fx("lfg.json");
const usual = fx("usual-week.json");
const viewer = { zone: usual.zone, cells: decodeCells(usual.cells) };
const start = new Date("2026-10-21T04:00:00Z");
const card = (id) => lfg.cards.find((c) => c.card_id === id);

// Independent expected value: walk the window and test each person's local cell.
function expectedOverlap(people) {
  const out = new Set();
  for (let j = 0; j < CELLS; j++) {
    const t = new Date(start.getTime() + j * 30 * 60000);
    if (people.every((p) => p.cells.has(usualCellOf(t, p.zone)))) out.add(j);
  }
  return out;
}
const asPerson = (m) => ({ zone: m.zone, cells: decodeCells(m.cells) });

test("fitCard for the pair card matches an independent count", () => {
  const c = card("12");
  const want = expectedOverlap([viewer, ...c.members.map(asPerson)]);
  const fit = fitCard(viewer, c, start);
  assert.deepEqual(fit.overlap, want);
  assert.equal(fit.hours, want.size / 2);
  assert.ok(fit.overlap.size > 0);
  assert.equal(fit.perMember.size, 2);
  assert.ok(fit.perMember.has("900000000000000011"));
});

test("fitCard for the London solo card converts through the member's zone", () => {
  const c = card("13");
  const want = expectedOverlap([viewer, asPerson(c.members[0])]);
  const fit = fitCard(viewer, c, start);
  assert.deepEqual(fit.overlap, want);
  assert.equal(fit.hours, want.size / 2);
});

test("rankCards puts the better fit first, ties by card_id", () => {
  const ranked = rankCards(viewer, lfg.cards, start);
  assert.equal(ranked.length, 2);
  assert.ok(ranked[0].fit.hours >= ranked[1].fit.hours);
  const hours = Object.fromEntries(ranked.map((r) => [r.card.card_id, r.fit.hours]));
  const best = hours["12"] >= hours["13"] ? "12" : "13";
  assert.equal(ranked[0].card.card_id, hours["12"] === hours["13"] ? "12" : best);
  const flipped = rankCards(viewer, [...lfg.cards].reverse(), start);
  assert.deepEqual(flipped.map((r) => r.card.card_id), ranked.map((r) => r.card.card_id));
});

test("rankCards breaks ties by card_id", () => {
  const twin = { ...card("13"), card_id: "11" };
  const ranked = rankCards(viewer, [card("13"), twin], start);
  assert.deepEqual(ranked.map((r) => r.card.card_id), ["11", "13"]);
});
