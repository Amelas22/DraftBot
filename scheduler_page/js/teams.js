// Find a team, the decisions: how well the viewer fits each team, what the join button may do, and what a
// refusal says. No DOM; teamsview.js draws it.
import { CELL_MINUTES, CELL_MS, decodeCells, windowCells } from "./week.js";
import { LinkExpired, NeedsUpdate, NEEDS_UPDATE } from "./api.js";
import { expiredMessage } from "./messages.js";
import { andList, plural } from "./words.js";

export const FORMAT = "draftbot.scheduler.teams";
export const NOTE_MAX = 200;
export const MULTI_TEAM_WARNING = "Being on more than one team needs a mod's permission. DM a mod first.";
export const NETWORK_FAILED = "Couldn't reach DraftBot. Check your connection and try again.";
const SERVER_FAILED = "DraftBot couldn't do that just now. Try again in a minute.";

export const halfHourFloor = (date) => new Date(Math.floor(date.getTime() / CELL_MS) * CELL_MS);
// The grid's first cell: prev while the clock is still in the same half-hour, else the new one, so a page left
// open keeps showing the next 7 days.
export function windowStart(prev, now) {
  const next = halfHourFloor(now);
  return prev && prev.getTime() === next.getTime() ? prev : next;
}
export const teamName = (team) => `${team.captain_name}'s team`;

// A member without a saved week (cells null) is never free.
const memberFree = (m, startUtc) => (m.cells && m.zone ? windowCells(m.zone, decodeCells(m.cells), startUtc) : new Set());

// people: [{ user, name, free }]; overlap = cells where every person is free.
function groupFit(people) {
  const overlap = new Set([...(people[0]?.free ?? [])].filter((j) => people.every((p) => p.free.has(j))));
  return { overlap, hours: (overlap.size * CELL_MINUTES) / 60, people };
}

// viewer: { user?, zone, cells: Set } (usual week). people[0] is the viewer as "You"; the viewer's own member row
// is skipped so a captain isn't counted twice. overlap = cells where every person is free.
export function fitTeam(viewer, team, startUtc) {
  const people = [{ user: viewer.user, name: "You", free: windowCells(viewer.zone, viewer.cells, startUtc) }];
  for (const m of team.members) {
    if (viewer.user !== undefined && m.user === viewer.user) continue;
    people.push({ user: m.user, name: m.name, free: memberFree(m, startUtc) });
  }
  return groupFit(people);
}

const idOrder = (a, b) => a.localeCompare(b, "en", { numeric: true });

// [{ team, hours, fit }], most shared hours first, ties by team_id.
export function rankTeams(viewer, teams, startUtc) {
  return teams
    .map((team) => {
      const fit = fitTeam(viewer, team, startUtc);
      return { team, hours: fit.hours, fit };
    })
    .sort((a, b) => b.hours - a.hours || idOrder(a.team.team_id, b.team.team_id));
}

export const onOtherTeam = (state) => state.me.on_teams.length > 0;
export const isFull = (team) => team.status === "full" || team.members.length >= team.size;
export const slotsText = (team) => `${team.members.length} of ${team.size}`;
// The teams the viewer is on, captain or not, in team_id order.
export const myTeams = (state) => state.teams
  .filter((t) => t.members.some((m) => m.user === state.me.user))
  .sort((a, b) => idOrder(a.team_id, b.team_id));

// Pending requests to a team the viewer captains: [{ request, hours, fit }], most hours shared with the whole
// team first, then whoever asked first. fit.people is the team (the viewer first, as "You") plus the requester,
// each from their saved week, which is what the captain's grid draws when a request is picked.
export function requestsFor(state, teamId, startUtc) {
  const team = state.teams.find((t) => t.team_id === teamId);
  const pending = state.me.incoming?.[teamId] ?? [];
  if (!team) return [];
  const row = (m, name = m.name) => ({ user: m.user, name, free: memberFree(m, startUtc) });
  const members = [
    ...team.members.filter((m) => m.user === state.me.user).map((m) => row(m, "You")),
    ...team.members.filter((m) => m.user !== state.me.user).map((m) => row(m)),
  ];
  return pending
    .map((request) => {
      const fit = groupFit([...members, row(request)]);
      return { request, hours: fit.hours, fit };
    })
    .sort((a, b) => b.hours - a.hours || a.request.requested_at.localeCompare(b.request.requested_at) || idOrder(a.request.user, b.request.user));
}

export const requestHeading = (request, hours) => `${request.name} with your team: ${plural(hours, "hour")} a week`;
export const CLOSE_CONFIRM = "Close your team? Everyone on it and everyone who asked to join gets a DM.";
export const leaveConfirm = (team) => `Leave ${teamName(team)}? ${team.captain_name} gets a DM.`;

// Keeps a refresh GET from undoing an action: a refresh isn't started while an action is in flight, and its
// answer is dropped if an action started or finished after it began (the action's answer is newer).
export function createSync() {
  let inFlight = 0;
  let finished = 0;
  return {
    startAction() {
      inFlight++;
      let done = false;
      return () => { if (!done) { done = true; inFlight--; finished++; } };
    },
    startRefresh: () => (inFlight > 0 ? null : { seen: finished }),
    isCurrent: (ticket) => ticket !== null && inFlight === 0 && ticket.seen === finished,
  };
}

// What the viewer typed to each team, so switching teams never carries a note to the wrong captain.
export function noteDrafts() {
  const drafts = new Map();
  return {
    get: (teamId) => drafts.get(teamId) ?? "",
    set: (teamId, text) => { drafts.set(teamId, text); },
    clear: (teamId) => { drafts.delete(teamId); },
  };
}

// What the viewer can do about one team: member (captain or not), requested, full, paused or open. An open team
// whose captain declined the viewer's latest request says so (declined: true); the viewer may ask again.
export function joinState(state, team) {
  if (team.members.some((m) => m.user === state.me.user)) return { kind: "member", captain: team.captain === state.me.user };
  const latest = state.me.outgoing.find((r) => r.team_id === team.team_id);
  if (latest?.status === "pending") return { kind: "requested" };
  if (team.status === "full" || team.members.length >= team.size) return { kind: "full" };
  if (!team.taking_requests) return { kind: "paused" };
  return latest?.status === "declined" ? { kind: "open", declined: true } : { kind: "open" };
}

export const declinedText = (team) => `${teamName(team)} declined your request.`;

// The line under the grid when there is no join button, or null for none: a full team's captain already sees
// the full banner in their list.
export function joinNote(js, team) {
  if (js.kind === "member") {
    if (!js.captain) return "You're on this team.";
    return isFull(team) ? null : "This is your team. You get a DM when someone asks to join.";
  }
  return js.kind === "full" ? "This team is full." : "This team isn't taking requests right now.";
}

// Picking a request shows the grid with that player added, so it always shows everyone.
export const modeAfterSelect = (key, mode) => (key.startsWith("r:") ? "everyone" : mode);

export const noteError = (note) => (note.length > NOTE_MAX ? `Keep the note to ${NOTE_MAX} characters or fewer.` : null);

const REFUSALS = {
  team_full: "That team just filled up.",
  not_taking_requests: "That team isn't taking requests right now.",
  already_member: "You're already on that team.",
  team_closed: "That team has closed.",
  request_gone: "That request was already answered.",
  bad_input: "Something about that request wasn't valid. Reload and try again.",
  not_captain: "Only the captain can do that.",
  too_many_teams: "You already captain two teams. Close one to open another.",
};
export const refusalText = (code) => (Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : SERVER_FAILED);

// A thrown POST: an expired link, an answer this page can't read, a refusal sent as an HTTP error (bad_input is a 400), no answer at all, or
// any other server failure.
export function actionErrorText(e) {
  if (e instanceof LinkExpired) return expiredMessage();
  if (e instanceof NeedsUpdate) return NEEDS_UPDATE;
  if (e && Object.hasOwn(REFUSALS, e.code)) return REFUSALS[e.code];
  if (!e || e.status === undefined) return NETWORK_FAILED;
  return SERVER_FAILED;
}

// "<time>: 2 of 3 free (You and Rook; not Fern)".
export function cellText(time, people, j) {
  const free = people.filter((p) => p.free.has(j)).map((p) => p.name);
  const busy = people.filter((p) => !p.free.has(j)).map((p) => p.name);
  const who = [free.length ? andList(free) : "", busy.length ? `not ${andList(busy)}` : ""].filter(Boolean).join("; ");
  return `${time}: ${free.length} of ${people.length} free (${who})`;
}

// The --h1..--h6 ramp step for n of total people free: 0 is nobody, 6 is everyone.
export const shadeLevel = (n, total) => (n <= 0 ? 0 : n >= total ? 6 : Math.min(5, Math.max(1, Math.round((n / total) * 6))));
