import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { weekStatusLabel, statusRows, mountConfirmPanel } from "../js/confirm.js";
import { CONFIRMATION_FORMAT, LinkExpired } from "../js/api.js";
import { expiredMessage } from "../js/messages.js";

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
  // Its own class: "confirm" is the teams page's yes/no box.
  assert.equal(root.children[0].className, "confirmpanel");
});

test("onChange runs after a saved change, not after a failed one", async () => {
  const root = new Node();
  const api = fakeApi(false);
  let changed = 0;
  await mountConfirmPanel(root, api, { ...opts, onChange: () => { changed += 1; } });
  await fire(button(root), "click");
  assert.equal(changed, 1);
  api.fail = true;
  checkbox(root).checked = true;
  await fire(checkbox(root), "change");
  assert.equal(changed, 1);
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

test("a double click confirms once", async () => {
  const root = new Node();
  const api = fakeApi(false);
  await mountConfirmPanel(root, api, opts);
  const b = button(root);
  await Promise.all([fire(b, "click"), fire(b, "click")]);
  assert.equal(api.calls.filter((c) => c[0] === "confirm").length, 1);
});

test("rapid switch toggles make one call while one is pending", async () => {
  const root = new Node();
  const api = fakeApi(true);
  await mountConfirmPanel(root, api, opts);
  checkbox(root).checked = false;
  await Promise.all([fire(checkbox(root), "change"), fire(checkbox(root), "change")]);
  assert.equal(api.calls.length, 1);
});

test("the controls are disabled while a call is pending", async () => {
  const root = new Node();
  const api = fakeApi(false);
  let release;
  api.confirmRound = () => new Promise((r) => { release = r; });
  await mountConfirmPanel(root, api, opts);
  const b = button(root);
  const pending = fire(b, "click");
  assert.equal(checkbox(root).disabled, true);
  assert.equal(b.disabled, true);
  release({ format: CONFIRMATION_FORMAT, version: 1, trusts_usual: false, confirmed_rounds: ["2026-11-16"] });
  await pending;
  assert.equal(checkbox(root).disabled, false);
});

test("the note is a polite live region and announces a confirm", async () => {
  const root = new Node();
  await mountConfirmPanel(root, fakeApi(false), opts);
  await fire(button(root), "click");
  const note = root.find((n) => n.attrs.role === "status");
  assert.equal(note.attrs["aria-live"], "polite");
  assert.equal(note.textContent, "Round 3: Confirmed ✓");
});

test("a confirm is only announced: the row already shows it, so the note is visually hidden", async () => {
  const root = new Node();
  await mountConfirmPanel(root, fakeApi(false), opts);
  await fire(button(root), "click");
  const note = root.find((n) => n.attrs.role === "status");
  assert.ok(note.className.split(" ").includes("sronly"));
});

test("a failure note after a hidden announcement is visible again", async () => {
  const root = new Node();
  const api = fakeApi(false);
  await mountConfirmPanel(root, api, opts);
  api.setTrustsUsual = async () => { throw new Error("down"); };
  await fire(button(root), "click");
  await fire(checkbox(root), "change");
  const note = root.find((n) => n.attrs.role === "status");
  assert.equal(note.textContent, "Couldn't save that. Check your connection and try again.");
  assert.ok(!note.className.split(" ").includes("sronly"));
});

test("an expired link on reload is visible even after a hidden announcement", async () => {
  const root = new Node();
  const api = fakeApi(false);
  const panel = await mountConfirmPanel(root, api, opts);
  await fire(button(root), "click");
  api.getConfirmation = async () => { throw new LinkExpired(); };
  await panel.reload();
  const note = root.find((n) => n.attrs.role === "status");
  assert.equal(note.textContent, expiredMessage());
  assert.ok(!note.className.split(" ").includes("sronly"));
});

test("a failed confirm shows the failure and keeps the row", async () => {
  const root = new Node();
  const api = fakeApi(false);
  api.confirmRound = async () => { throw new Error("down"); };
  await mountConfirmPanel(root, api, opts);
  await fire(button(root), "click");
  assert.ok(texts(root).includes("Couldn't save that. Check your connection and try again."));
  assert.ok(texts(root).includes("Round 3: Not confirmed"));
  assert.ok(button(root));
});

test("an expired link says so", async () => {
  const root = new Node();
  const api = fakeApi(false);
  api.confirmRound = async () => { throw new LinkExpired(); };
  await mountConfirmPanel(root, api, opts);
  await fire(button(root), "click");
  assert.ok(texts(root).includes(expiredMessage()));
});

test("a failed or good reload leaves a visible failure note alone", async () => {
  const root = new Node();
  const api = fakeApi(false);
  api.confirmRound = async () => { throw new Error("down"); };
  const panel = await mountConfirmPanel(root, api, opts);
  await fire(button(root), "click");
  await panel.reload();
  assert.ok(texts(root).includes("Couldn't save that. Check your connection and try again."));
  api.getConfirmation = async () => { throw new Error("down"); };
  await panel.reload();
  assert.ok(texts(root).includes("Couldn't save that. Check your connection and try again."));
});
