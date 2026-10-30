import test from "node:test";
import assert from "node:assert/strict";
import { LinkExpired } from "../js/api.js";
import { mountTeamDraft } from "../js/teamdraftview.js";

// A just-enough DOM for the view's first paint: the matchup GET answers "link expired" so only the loading
// state and the notice are ever built.
const node = () => ({ append() {}, setAttribute() {}, replaceChildren() {}, classList: { remove() {} } });
globalThis.document = { createElement: node };

const mount = async (deps) => {
  const gets = [];
  const api = {
    getUsual: async () => ({}),
    get: async (path) => { gets.push(path); throw new LinkExpired(); },
  };
  await mountTeamDraft(node(), api, deps);
  return gets;
};

test("mounting with no paths fetches the team-draft matchup", async () => {
  assert.deepEqual(await mount(), ["/api/team-draft"]);
});

test("mounting with paths fetches the matchup they name", async () => {
  const paths = { get: "/api/tournament-match", actions: "/api/tournament-match/actions" };
  assert.deepEqual(await mount({ paths }), ["/api/tournament-match"]);
});
