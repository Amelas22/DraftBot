import { NeedsUpdate, NotEnabled } from "./api.js";

export const LOAD_FAILED = "Something went wrong loading this page. Try the link again.";
export const NOT_ENABLED = "Find a team is turned off in this server right now.";
// What a page that failed to load says: an out-of-date page or a switched-off server says so, anything else is LOAD_FAILED.
export const loadFailedMessage = (e) => {
  if (e instanceof NotEnabled) return NOT_ENABLED;
  return e instanceof NeedsUpdate ? e.message : LOAD_FAILED;
};

// A 401 can arrive before the page knows which view the link was for, so every expired link says the same thing.
export const EXPIRED = "This link has expired. Press Find a team or My usual week in Discord for a new one.";
export const expiredMessage = () => EXPIRED;
