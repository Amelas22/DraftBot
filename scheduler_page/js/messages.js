import { NeedsUpdate } from "./api.js";

export const LOAD_FAILED = "Something went wrong loading this page. Try the link again.";
// What a page that failed to load says: an out-of-date page says so, anything else is LOAD_FAILED.
export const loadFailedMessage = (e) => (e instanceof NeedsUpdate ? e.message : LOAD_FAILED);

// What a dead link says. Before the link file is read the view is unknown, so the wording is neutral;
// once it is known, each view names the button that hands out a fresh link.
const AGAIN = {
  usual: "Press My usual week in /settings for a new one.",
  lfg: "Press How well do we fit? on a card in #find-a-team for a new one.",
};
const NEUTRAL = "Press the button in Discord again for a new one.";

const again = (view) => (Object.hasOwn(AGAIN, view) ? AGAIN[view] : NEUTRAL);

export const expiredMessage = (view) => `This link has expired. ${again(view)}`;
export const brokenMessage = (view) => `This link is broken. ${again(view)}`;
