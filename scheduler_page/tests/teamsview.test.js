import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LinkExpired } from "../js/api.js";
import { mountTeams } from "../js/teamsview.js";
import { REGISTRATION_CLOSED } from "../js/teams.js";
import { expiredMessage } from "../js/messages.js";

const usual = JSON.parse(readFileSync(new URL("../fixtures/usual-week.json", import.meta.url)));
const now = () => new Date("2026-10-25T00:00:00Z");

// A just-enough DOM: the page's first load ends in a single notice paragraph, which is all these tests read.
class Node {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.textContent = ""; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get classList() { return { add() {}, remove() {}, toggle() {} }; }
}

let saved;
before(() => {
  saved = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = {
    createElement: () => new Node(), visibilityState: "hidden", addEventListener() {}, removeEventListener() {},
  };
});
after(() => {
  if (saved) Object.defineProperty(globalThis, "document", saved);
  else delete globalThis.document;
});

// An api whose teams read fails the given way; the usual week loads fine, so the page reaches the list.
const refusingApi = (err) => ({
  getUsual: async () => usual,
  get: async () => { throw err; },
  post: async () => { throw new Error("no action expected"); },
});
const firstLoad = async (err) => {
  const root = new Node();
  await mountTeams(root, refusingApi(err), { now });
  assert.equal(root.children.length, 1);
  return root.children[0];
};

test("a link opened after registration closed shows when Find a team opens again, on the first load", async () => {
  const notice = await firstLoad(Object.assign(new Error("x"), { code: "registration_closed", status: 403 }));
  assert.equal(notice.className, "notice");
  assert.equal(notice.textContent, REGISTRATION_CLOSED);
});

test("an expired link on the first load still shows the expired message", async () => {
  const notice = await firstLoad(new LinkExpired());
  assert.equal(notice.textContent, expiredMessage());
});

test("any other first-load failure still surfaces", async () => {
  await assert.rejects(firstLoad(Object.assign(new Error("boom"), { code: "http_500", status: 500 })), /boom/);
});
