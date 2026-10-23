import { NeedsUpdate } from "./api.js";

export const LOAD_FAILED = "Something went wrong loading this page. Try the link again.";
// What a page that failed to load says: an out-of-date page says so, anything else is LOAD_FAILED.
export const loadFailedMessage = (e) => (e instanceof NeedsUpdate ? e.message : LOAD_FAILED);

// A 401 can arrive before the page knows which view the link was for, so every expired link says the same thing.
export const EXPIRED = "This link has expired. Press Find a team or My usual week in Discord for a new one.";
export const expiredMessage = () => EXPIRED;
