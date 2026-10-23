// One request at a time: PUT file 4, then poll file 6 until the bot answers that request_id.
// Fixture mode never writes; it answers with fake(request) after FAKE_MS. Timers and getMe are injectable.
import { LinkExpired } from "./api.js";

export const POLL_MS = 5000;
export const SLOW_MS = 120000;
export const FAKE_MS = 1500;
export const SLOW_MESSAGE = "DraftBot hasn't answered yet; it will, or check the match thread";

const defaultTimer = (fn, ms) => {
  const id = setTimeout(fn, ms);
  return () => clearTimeout(id);
};

// opts: { putUrl, put(url, body), getMe() -> me file (parsed), fixture, fake(request) -> {status, reason},
//         now() -> Date, setTimer(fn, ms) -> cancel() }
// A LinkExpired from getMe ends the request: polling stops, state().error holds it and send() rejects with it.
export function createRequester(opts) {
  const { putUrl, getMe, fixture, fake } = opts;
  const now = opts.now || (() => new Date());
  const setTimer = opts.setTimer || defaultTimer;
  const put = opts.put;

  let pending = false, request = null, outcome = null, error = null, since = 0, slowFlag = false;
  const cancels = new Set();
  const listeners = new Set();

  const state = () => ({ pending, request, outcome, error, slow: pending && (slowFlag || now().getTime() - since >= SLOW_MS) });
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
    error = null;
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
      const fail = (e) => {
        stopTimers();
        pending = false;
        error = e;
        emit();
        reject(e);
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
          if (e instanceof LinkExpired) return fail(e);
          console.warn("polling the match file failed; trying again", e);
        }
        if (pending) later(poll, POLL_MS);
      };

      put(putUrl, req).then(() => {
        later(() => { slowFlag = true; emit(); }, SLOW_MS);
        later(poll, POLL_MS);
      }, (e) => {
        request = null;
        fail(e);
      });
    });
  }

  return {
    send,
    state,
    // Drop every timer (the view is going away); a pending send never settles.
    stop: stopTimers,
    onChange(cb) { listeners.add(cb); return () => listeners.delete(cb); },
  };
}
