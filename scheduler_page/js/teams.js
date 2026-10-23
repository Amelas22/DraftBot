// Find a team, the decisions: how well the viewer fits each team, what the join button may do, and what a
// refusal says. No DOM; teamsview.js draws it.
import { CELL_MINUTES, CELL_MS, decodeCells, windowCells } from "./week.js";
import { LinkExpired } from "./api.js";
import { expiredMessage } from "./messages.js";
import { andList } from "./words.js";

export const FORMAT = "draftbot.scheduler.teams";
export const NOTE_MAX = 200;
export const MULTI_TEAM_WARNING = "Being on more than one team needs a mod's permission. DM a mod first.";
export const NETWORK_FAILED = "Couldn't reach DraftBot. Check your connection and try again.";
const SERVER_FAILED = "DraftBot couldn't do that just now. Try again in a minute.";

export const halfHourFloor = (date) => new Date(Math.floor(date.getTime() / CELL_MS) * CELL_MS);
export const teamName = (team) => `${team.captain_name}'s team`;

// A member without a saved week (cells null) is never free.
const memberFree = (m, startUtc) => (m.cells && m.zone ? windowCells(m.zone, decodeCells(m.cells), startUtc) : new Set());

// viewer: { user?, zone, cells: Set } (usual week). people[0] is the viewer as "You"; the viewer's own member row
// is skipped so a captain isn't counted twice. overlap = cells where every person is free.
export function fitTeam(viewer, team, startUtc) {
  const people = [{ user: viewer.user, name: "You", free: windowCells(viewer.zone, viewer.cells, startUtc) }];
  for (const m of team.members) {
    if (viewer.user !== undefined && m.user === viewer.user) continue;
    people.push({ user: m.user, name: m.name, free: memberFree(m, startUtc) });
  }
  const overlap = new Set([...people[0].free].filter((j) => people.every((p) => p.free.has(j))));
  return { overlap, hours: (overlap.size * CELL_MINUTES) / 60, people };
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

// What the viewer can do about one team: member (captain or not), requested, full, paused or open.
export function joinState(state, team) {
  if (team.members.some((m) => m.user === state.me.user)) return { kind: "member", captain: team.captain === state.me.user };
  if (state.me.outgoing.some((r) => r.team_id === team.team_id && r.status === "pending")) return { kind: "requested" };
  if (team.status === "full" || team.members.length >= team.size) return { kind: "full" };
  if (!team.taking_requests) return { kind: "paused" };
  return { kind: "open" };
}

export const noteError = (note) => (note.length > NOTE_MAX ? `Keep the note to ${NOTE_MAX} characters or fewer.` : null);

const REFUSALS = {
  team_full: "That team just filled up.",
  not_taking_requests: "That team isn't taking requests right now.",
  already_member: "You're already on that team.",
  team_closed: "That team has closed.",
  request_gone: "That request was already answered.",
  bad_input: "Something about that request wasn't valid. Reload and try again.",
  not_captain: "Only the captain can do that.",
};
export const refusalText = (code) => (Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : SERVER_FAILED);

// A thrown POST: an expired link, a refusal sent as an HTTP error (bad_input is a 400), no answer at all, or
// any other server failure.
export function actionErrorText(e) {
  if (e instanceof LinkExpired) return expiredMessage();
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
