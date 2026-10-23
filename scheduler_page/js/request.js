// One request at a time: PUT file 4, then poll file 6 until the bot answers that request_id.
// Fixture mode never writes; it answers with fake(request) after FAKE_MS. Timers and getMe are injectable.
import { putJson } from "./api.js";

export const POLL_MS = 5000;
export const SLOW_MS = 120000;
export const FAKE_MS = 1500;
export const SLOW_MESSAGE = "DraftBot hasn't answered yet; it will, or check the match thread";

const defaultTimer = (fn, ms) => {
  const id = setTimeout(fn, ms);
  return () => clearTimeout(id);
};

// opts: { putUrl, getMe() -> me file (parsed), fixture, fake(request) -> {status, reason}, now() -> Date,
//         setTimer(fn, ms) -> cancel(), put?(url, body) }
export function createRequester(opts) {
  const { putUrl, getMe, fixture, fake } = opts;
  const now = opts.now || (() => new Date());
  const setTimer = opts.setTimer || defaultTimer;
  const put = opts.put || putJson;

  let pending = false, request = null, outcome = null, since = 0, slowFlag = false;
  const cancels = new Set();
  const listeners = new Set();

  const state = () => ({ pending, request, outcome, slow: pending && (slowFlag || now().getTime() - since >= SLOW_MS) });
  const emit = () => { const s = state(); for (const cb of listeners) cb(s); };
  const later = (fn, ms) => {
    const cancel = setTimer(() => { cancels.delete(cancel); return fn(); }, ms);
    cancels.add(cancel);
  };
  const stopTimers = () => { for (const c of cancels) c(); cancels.clear(); };

  function send(req) {
    if (pending) return Promise.reject(new Error("A request is already pending"));
    pending = true;
    request = req;
    outcome = null;
    slowFlag = false;
    since = now().getTime();
    emit();

    return new Promise((resolve, reject) => {
      const finish = (o) => {
        stopTimers();
        pending = false;
        outcome = { status: o.status, reason: o.reason ?? null };
        emit();
        resolve(outcome);
      };

      if (fixture) {
        later(() => finish(fake(req)), FAKE_MS);
        return;
      }

      const poll = async () => {
        try {
          const me = await getMe();
          const answer = (me && me.requests || []).find((x) => x.request_id === req.request_id);
          if (answer && (answer.status === "booked" || answer.status === "refused")) return finish(answer);
        } catch (e) {
          console.warn("polling the match file failed; trying again", e);
        }
        if (pending) later(poll, POLL_MS);
      };

      put(putUrl, req).then(() => {
        later(() => { slowFlag = true; emit(); }, SLOW_MS);
        later(poll, POLL_MS);
      }, (e) => {
        pending = false;
        request = null;
        emit();
        reject(e);
      });
    });
  }

  return {
    send,
    state,
    onChange(cb) { listeners.add(cb); return () => listeners.delete(cb); },
  };
}
