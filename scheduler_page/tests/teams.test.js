import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  fitTeam, rankTeams, halfHourFloor, onOtherTeam, joinState, noteError, refusalText, actionErrorText,
  cellText, declinedText, joinNote, modeAfterSelect, shadeLevel, teamName, MULTI_TEAM_WARNING, NETWORK_FAILED,
  isFull, requestsFor, myTeams, requestHeading, slotsText, createSync, windowStart, noteDrafts,
  CLOSE_CONFIRM, leaveConfirm, membersText,
} from "../js/teams.js";
import { decodeCells, usualCellOf, CELLS } from "../js/week.js";
import { LinkExpired, NeedsUpdate, NEEDS_UPDATE } from "../js/api.js";
import { EXPIRED } from "../js/messages.js";

const fx = (n) => JSON.parse(readFileSync(new URL(`../fixtures/${n}`, import.meta.url)));
const state = fx("teams.json");
const usual = fx("usual-week.json");
const viewer = { user: state.me.user, zone: usual.zone, cells: decodeCells(usual.cells) };
const start = new Date("2026-10-21T04:00:00Z");
const team = (id) => state.teams.find((t) => t.team_id === id);

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

test("fitTeam matches an independent count across zones", () => {
  const t = team("101");
  const want = expectedOverlap([viewer, ...t.members.map(asPerson)]);
  const fit = fitTeam(viewer, t, start);
  assert.ok(want.size > 0);
  assert.deepEqual(fit.overlap, want);
  assert.equal(fit.hours, want.size / 2);
  assert.deepEqual(fit.people.map((p) => p.name), ["You", "Rook", "Wren"]);
});

test("a member with no saved week is never free: hours 0, no throw", () => {
  const fit = fitTeam(viewer, team("103"), start);
  assert.equal(fit.hours, 0);
  const fern = fit.people.find((p) => p.name === "Fern");
  assert.equal(fern.free.size, 0);
});

test("the viewer is not counted twice on their own team", () => {
  const own = { ...team("102"), members: [...team("102").members, { user: viewer.user, name: "Ben", zone: usual.zone, cells: usual.cells }] };
  const fit = fitTeam(viewer, own, start);
  assert.deepEqual(fit.people.map((p) => p.name), ["You", "Moss"]);
});

test("rankTeams: most hours first, ties by team_id", () => {
  const ranked = rankTeams(viewer, state.teams, start);
  const hours = (id) => expectedOverlap([viewer, ...team(id).members.map(asPerson)]).size / 2;
  assert.ok(hours("101") > hours("102") && hours("102") > 0);
  assert.deepEqual(ranked.map((r) => [r.team.team_id, r.hours]), [["101", hours("101")], ["102", hours("102")], ["103", 0]]);
  assert.deepEqual(rankTeams(viewer, [...state.teams].reverse(), start).map((r) => r.team.team_id), ["101", "102", "103"]);
  const twin = { ...team("103"), team_id: "99" };
  assert.deepEqual(rankTeams(viewer, [team("103"), twin], start).map((r) => r.team.team_id), ["99", "103"]);
});

test("halfHourFloor", () => {
  assert.equal(halfHourFloor(new Date("2026-10-23T10:44:12Z")).toISOString(), "2026-10-23T10:30:00.000Z");
});

test("onOtherTeam is true when the viewer is on any team", () => {
  assert.equal(onOtherTeam(state), false);
  assert.equal(onOtherTeam({ ...state, me: { ...state.me, on_teams: ["7"] } }), true);
  assert.equal(MULTI_TEAM_WARNING, "Being on more than one team needs a mod's permission. DM a mod first.");
});

test("joinState covers member, captain, requested, full, paused and open", () => {
  const me = (extra) => ({ ...state, me: { ...state.me, ...extra } });
  assert.deepEqual(joinState(state, team("101")), { kind: "open" });
  const mine = { ...team("101"), captain: viewer.user, members: [...team("101").members, { user: viewer.user, name: "Ben" }] };
  assert.deepEqual(joinState(state, mine), { kind: "member", captain: true });
  assert.deepEqual(joinState(state, { ...mine, captain: "1" }), { kind: "member", captain: false });
  assert.deepEqual(joinState(me({ outgoing: [{ team_id: "101", status: "pending", note: "" }] }), team("101")), { kind: "requested" });
  assert.deepEqual(joinState(me({ outgoing: [{ team_id: "101", status: "withdrawn", note: "" }] }), team("101")), { kind: "open" });
  assert.deepEqual(joinState(state, { ...team("101"), size: 2 }), { kind: "full" });
  assert.deepEqual(joinState(state, { ...team("101"), status: "full" }), { kind: "full" });
  assert.deepEqual(joinState(state, { ...team("101"), taking_requests: false }), { kind: "paused" });
});

test("a declined request leaves the team open to ask again, and says so", () => {
  const me = (status) => ({ ...state, me: { ...state.me, outgoing: [{ team_id: "101", status, note: "" }] } });
  assert.deepEqual(joinState(me("declined"), team("101")), { kind: "open", declined: true });
  assert.deepEqual(joinState(me("withdrawn"), team("101")), { kind: "open" });
  assert.deepEqual(joinState(me("declined"), team("102")), { kind: "open" });
  assert.deepEqual(joinState(me("declined"), { ...team("101"), status: "full" }), { kind: "full" });
  assert.equal(declinedText(team("101")), "Rook's team declined your request.");
});

test("a note over 200 characters is refused before sending", () => {
  assert.equal(noteError(""), null);
  assert.equal(noteError("x".repeat(200)), null);
  assert.match(noteError("x".repeat(201)), /200/);
});

test("refusal codes read as sentences", () => {
  assert.equal(refusalText("team_full"), "That team just filled up.");
  assert.equal(refusalText("not_taking_requests"), "That team isn't taking requests right now.");
  assert.equal(refusalText("already_member"), "You're already on that team.");
  assert.equal(refusalText("team_closed"), "That team has closed.");
  assert.equal(refusalText("request_gone"), "That request was already answered.");
  assert.equal(refusalText("bad_input"), "Something about that request wasn't valid. Reload and try again.");
  assert.equal(refusalText("not_captain"), "Only the captain can do that.");
  assert.equal(refusalText("too_many_teams"), "You already captain two teams. Close one to open another.");
  assert.equal(typeof refusalText("something_new"), "string");
});

test("action errors: expired, refusal over HTTP, network, server", () => {
  assert.equal(actionErrorText(new LinkExpired()), EXPIRED);
  assert.equal(actionErrorText(Object.assign(new Error("x"), { code: "bad_input", status: 400 })), refusalText("bad_input"));
  assert.equal(actionErrorText(new TypeError("Failed to fetch")), NETWORK_FAILED);
  assert.equal(NETWORK_FAILED, "Couldn't reach DraftBot. Check your connection and try again.");
  assert.notEqual(actionErrorText(Object.assign(new Error("x"), { code: "http_500", status: 500 })), NETWORK_FAILED);
});

test("cellText names who is free and who isn't", () => {
  const people = [
    { name: "You", free: new Set([1, 2]) },
    { name: "Rook", free: new Set([2]) },
    { name: "Fern", free: new Set() },
  ];
  assert.equal(cellText("Wed 21 Oct, 7:00 PM", people, 2), "Wed 21 Oct, 7:00 PM: 2 of 3 free (You and Rook; not Fern)");
  assert.equal(cellText("T", people, 0), "T: 0 of 3 free (not You, Rook and Fern)");
  assert.equal(cellText("T", people.slice(0, 1), 1), "T: 1 of 1 free (You)");
});

test("shadeLevel: 0 for nobody, 6 for everyone, rising in between", () => {
  assert.equal(shadeLevel(0, 3), 0);
  assert.equal(shadeLevel(3, 3), 6);
  assert.equal(shadeLevel(1, 1), 6);
  const four = [0, 1, 2, 3, 4].map((n) => shadeLevel(n, 4));
  for (let i = 1; i < four.length; i++) assert.ok(four[i] > four[i - 1]);
});

test("teamName", () => {
  assert.equal(teamName(team("101")), "Rook's team");
});

test("membersText names everyone on the team but the captain, the viewer as you", () => {
  assert.equal(membersText(team("101"), state.me.user), "with Wren");
  assert.equal(membersText(team("102"), state.me.user), "");
  const three = { ...team("101"), members: [...team("101").members, { user: state.me.user, name: "Ben" }, { user: "9", name: "Ash" }] };
  assert.equal(membersText(three, state.me.user), "with Wren, you and Ash");
});

const captain = fx("teams-captain.json");

test("isFull: full status or no open slot", () => {
  assert.equal(isFull(team("101")), false);
  assert.equal(isFull({ ...team("101"), status: "full" }), true);
  assert.equal(isFull({ ...team("101"), size: 2 }), true);
});

test("requestsFor: most shared hours with the team first, then the earliest request", () => {
  const reqs = requestsFor(captain, "101", start);
  assert.deepEqual(reqs.map((r) => [r.request.name, r.hours]), [["Ben", 12], ["Kit", 5]]);
  const t = captain.teams.find((x) => x.team_id === "101");
  for (const r of reqs) {
    const want = expectedOverlap([...t.members.map(asPerson), asPerson(r.request)]);
    assert.deepEqual(r.fit.overlap, want);
  }
  // A tie on hours falls back to who asked first.
  const ben = captain.me.incoming["101"].find((r) => r.name === "Ben");
  const tie = { ...captain, me: { ...captain.me, incoming: { "101": [
    { ...ben, user: "1", name: "Late", requested_at: "2026-10-22T23:59:00Z" },
    { ...ben, user: "2", name: "Early", requested_at: "2026-10-22T01:00:00Z" },
  ] } } };
  assert.deepEqual(requestsFor(tie, "101", start).map((r) => r.request.name), ["Early", "Late"]);
  assert.deepEqual(requestsFor(captain, "102", start), []);
  assert.deepEqual(requestsFor({ ...captain, me: { ...captain.me, incoming: undefined } }, "101", start), []);
});

test("selecting a request draws the team plus the requester, the viewer first as You", () => {
  const [ben] = requestsFor(captain, "101", start);
  assert.deepEqual(ben.fit.people.map((p) => p.name), ["You", "Wren", "Ben"]);
  assert.equal(requestHeading(ben.request, ben.hours), "Ben with your team: 12 hours a week");
  assert.equal(requestHeading({ name: "Kit" }, 1), "Kit with your team: 1 hour a week");
});

test("a requester without a saved week fits for 0 hours", () => {
  const nobody = { ...captain, me: { ...captain.me, incoming: { "101": [{ user: "3", name: "Nox", note: "", zone: null, cells: null, requested_at: "2026-10-22T01:00:00Z" }] } } };
  const [r] = requestsFor(nobody, "101", start);
  assert.equal(r.hours, 0);
});

test("myTeams lists the teams the viewer is on, captained or not", () => {
  assert.deepEqual(myTeams(captain).map((t) => t.team_id), ["101"]);
  assert.deepEqual(myTeams(state), []);
  assert.equal(slotsText(team("101")), "2 of 3");
});

test("confirm texts", () => {
  assert.equal(CLOSE_CONFIRM, "Close your team? Everyone on it and everyone who asked to join gets a DM.");
  assert.equal(leaveConfirm(team("101")), "Leave Rook's team? Rook gets a DM.");
});

test("a refresh is skipped while an action is in flight", () => {
  const sync = createSync();
  const done = sync.startAction();
  assert.equal(sync.startRefresh(), null);
  done();
  assert.notEqual(sync.startRefresh(), null);
});

test("a refresh that began before an action finished is stale", () => {
  const sync = createSync();
  const ticket = sync.startRefresh();
  const done = sync.startAction();
  assert.equal(sync.isCurrent(ticket), false, "an action still in flight will bring newer state");
  done();
  assert.equal(sync.isCurrent(ticket), false, "the action's state is newer than this GET");
  const fresh = sync.startRefresh();
  assert.equal(sync.isCurrent(fresh), true);
  done(); // a second call is harmless
  assert.equal(sync.isCurrent(fresh), true);
});

test("windowStart keeps the same instant and moves with the clock", () => {
  const a = windowStart(null, new Date("2026-10-23T10:44:12Z"));
  assert.equal(a.toISOString(), "2026-10-23T10:30:00.000Z");
  assert.equal(windowStart(a, new Date("2026-10-23T10:59:59Z")), a);
  assert.equal(windowStart(a, new Date("2026-10-30T11:01:00Z")).toISOString(), "2026-10-30T11:00:00.000Z");
});

test("note drafts are kept per team", () => {
  const d = noteDrafts();
  d.set("101", "hi Rook");
  assert.equal(d.get("102"), "");
  d.set("102", "hi Moss");
  assert.equal(d.get("101"), "hi Rook");
  d.clear("101");
  assert.equal(d.get("101"), "");
  assert.equal(d.get("102"), "hi Moss");
});

test("an action answer this page can't read says the page needs updating", () => {
  assert.equal(actionErrorText(new NeedsUpdate()), NEEDS_UPDATE);
  assert.equal(NEEDS_UPDATE, "This page needs updating.");
});

test("joinNote: nothing under a full team's grid for its captain", () => {
  const t = team("101");
  const full = { ...t, status: "full" };
  assert.equal(joinNote({ kind: "member", captain: true }, t), "This is your team. You get a DM when someone asks to join.");
  assert.equal(joinNote({ kind: "member", captain: true }, full), null);
  assert.equal(joinNote({ kind: "member", captain: true }, { ...t, size: t.members.length }), null);
  assert.equal(joinNote({ kind: "member", captain: false }, full), "You're on this team.");
  assert.equal(joinNote({ kind: "full" }, full), "This team is full.");
  assert.equal(joinNote({ kind: "paused" }, t), "This team isn't taking requests right now.");
});

test("picking a request switches the grid to everyone; picking a team keeps the mode", () => {
  assert.equal(modeAfterSelect("r:101:55", "mine"), "everyone");
  assert.equal(modeAfterSelect("r:101:55", "everyone"), "everyone");
  assert.equal(modeAfterSelect("t:101", "mine"), "mine");
  assert.equal(modeAfterSelect("t:101", "everyone"), "everyone");
});
