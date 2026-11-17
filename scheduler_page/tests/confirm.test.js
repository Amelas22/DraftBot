import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { weekStatusLabel, statusRows, mountConfirmPanel } from "../js/confirm.js";
import { CONFIRMATION_FORMAT } from "../js/api.js";

test("weekStatusLabel", () => {
  assert.equal(weekStatusLabel("2026-11-16", 3), "Round 3");
  assert.equal(weekStatusLabel("2026-11-16", null), "Week ending Sun Nov 22");
});

test("statusRows", () => {
  const rows = statusRows({ trusts_usual: false, confirmed_rounds: ["2026-11-23"] }, ["2026-11-16", "2026-11-23"], null, new Date("2026-11-20T00:00:00Z"));
  assert.deepEqual(rows.map((r) => [r.text, r.canConfirm]), [
    ["Week ending Sun Nov 22: Not confirmed", true], ["Week ending Sun Nov 29: Confirmed ✓", false]]);
});

test("a number names the week only when there is one week", () => {
  const doc = { trusts_usual: false, confirmed_rounds: [] };
  const now = new Date("2026-11-20T00:00:00Z");
  assert.equal(statusRows(doc, ["2026-11-16"], 3, now)[0].text, "Round 3: Not confirmed");
  assert.equal(statusRows(doc, ["2026-11-16", "2026-11-23"], 3, now)[0].text, "Week ending Sun Nov 22: Not confirmed");
});

test("a finished week has no button", () =>
  assert.equal(statusRows({ trusts_usual: false, confirmed_rounds: [] }, ["2026-11-16"], 3, new Date("2026-11-23T08:00:00Z"))[0].canConfirm, false));

// A just-enough DOM.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.textContent = ""; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  find(test) { return test(this) ? this : this.children.map((c) => c.find?.(test)).find(Boolean); }
  findAll(test) { return [...(test(this) ? [this] : []), ...this.children.flatMap((c) => c.findAll?.(test) ?? [])]; }
}
let saved;
before(() => { saved = Object.getOwnPropertyDescriptor(globalThis, "document"); globalThis.document = { createElement: () => new Node() }; });
after(() => { if (saved) Object.defineProperty(globalThis, "document", saved); else delete globalThis.document; });

const texts = (root) => root.findAll((n) => n.textContent).map((n) => n.textContent);
const button = (root) => root.find((n) => n.textContent === "Looks right, confirm");
const checkbox = (root) => root.find((n) => n.attrs.type === "checkbox");
const fire = async (node, type) => { for (const fn of node.listeners[type]) await fn({}); };

function fakeApi(trusts, confirmed = []) {
  const state = { trusts_usual: trusts, confirmed_rounds: confirmed };
  const calls = [];
  const doc = () => ({ format: CONFIRMATION_FORMAT, version: 1, ...structuredClone(state) });
  return {
    calls,
    fail: false,
    getConfirmation: async () => doc(),
    setTrustsUsual: async function (v, range) { calls.push(["set", v, range]); if (this.fail) throw new Error("down"); state.trusts_usual = v; return doc(); },
    confirmRound: async (m, range) => { calls.push(["confirm", m, range]); state.confirmed_rounds.push(m); return doc(); },
  };
}
const opts = { weeks: ["2026-11-16"], number: 3, now: new Date("2026-11-18T00:00:00Z") };

test("switch on: no rows and no note", async () => {
  const root = new Node();
  await mountConfirmPanel(root, fakeApi(true), opts);
  assert.equal(checkbox(root).checked, true);
  assert.equal(button(root), undefined);
  assert.ok(!texts(root).includes("Editing any day in a round confirms that round."));
  assert.ok(texts(root).includes("Show my usual as confirmed"));
});

test("unchecking calls setTrustsUsual(false) and shows the round's row", async () => {
  const root = new Node();
  const api = fakeApi(true);
  await mountConfirmPanel(root, api, opts);
  checkbox(root).checked = false;
  await fire(checkbox(root), "change");
  assert.deepEqual(api.calls[0], ["set", false, { from: "2026-11-16", to: "2026-11-16" }]);
  assert.ok(texts(root).includes("Round 3: Not confirmed"));
  assert.ok(texts(root).includes("Editing any day in a round confirms that round."));
});

test("Looks right, confirm confirms that week", async () => {
  const root = new Node();
  const api = fakeApi(false);
  await mountConfirmPanel(root, api, opts);
  await fire(button(root), "click");
  assert.deepEqual(api.calls[0], ["confirm", "2026-11-16", { from: "2026-11-16", to: "2026-11-16" }]);
  assert.ok(texts(root).includes("Round 3: Confirmed ✓"));
  assert.equal(button(root), undefined);
});

test("a failed switch restores the checkbox and says so", async () => {
  const root = new Node();
  const api = fakeApi(true);
  api.fail = true;
  await mountConfirmPanel(root, api, opts);
  checkbox(root).checked = false;
  await fire(checkbox(root), "change");
  assert.equal(checkbox(root).checked, true);
  assert.ok(texts(root).includes("Couldn't save that. Check your connection and try again."));
});
