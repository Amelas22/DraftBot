import test from "node:test";
import assert from "node:assert/strict";
import { createRequester, POLL_MS, SLOW_MS, FAKE_MS } from "../js/request.js";

// A hand-cranked clock: timers fire only when advance() passes their due time.
function fakeClock(start = 0) {
  let t = start;
  let timers = [];
  const clock = {
    now: () => new Date(t),
    setTimer: (fn, ms) => {
      const timer = { due: t + ms, fn, live: true };
      timers.push(timer);
      return () => { timer.live = false; };
    },
    async advance(ms) {
      const end = t + ms;
      await flush();
      for (;;) {
        const next = timers.filter((x) => x.live && x.due <= end).sort((a, b) => a.due - b.due)[0];
        if (!next) break;
        t = next.due;
        next.live = false;
        await next.fn();
        await flush();
      }
      t = end;
      await flush();
    },
    pending: () => timers.filter((x) => x.live).length,
  };
  return clock;
}
const flush = () => new Promise((r) => setImmediate(r));

const REQ = { format: "draftbot.scheduler.request", version: 1, request_id: "r-1", kind: "book_draft", start_utc: "2026-10-20T23:00:00Z" };
const meWith = (...requests) => ({ requests });

function realSetup(answers) {
  const clock = fakeClock();
  const puts = [];
  let calls = 0;
  const getMe = async () => {
    const a = answers[Math.min(calls, answers.length - 1)];
    calls++;
    if (a instanceof Error) throw a;
    return a;
  };
  const put = async (url, body) => { puts.push({ url, body }); };
  const r = createRequester({ putUrl: "https://put", getMe, fixture: false, now: clock.now, setTimer: clock.setTimer, put });
  return { r, clock, puts, getMeCalls: () => calls };
}

test("real mode: PUT, then pending until file 6 answers booked", async () => {
  const { r, clock, puts, getMeCalls } = realSetup([
    meWith(),
    meWith({ request_id: "r-1", status: "pending", reason: null }),
    meWith({ request_id: "r-0", status: "refused", reason: "stale" }, { request_id: "r-1", status: "booked", reason: null }),
  ]);
  const seen = [];
  r.onChange((s) => seen.push(s.pending));
  const done = r.send(REQ);
  await flush();
  assert.deepEqual(puts, [{ url: "https://put", body: REQ }]);
  assert.equal(r.state().pending, true);
  assert.equal(r.state().request, REQ);
  assert.equal(getMeCalls(), 0, "polls only after the first interval");
  await clock.advance(POLL_MS);
  assert.equal(r.state().pending, true);
  await clock.advance(POLL_MS);
  assert.equal(r.state().pending, true);
  await clock.advance(POLL_MS);
  assert.deepEqual(await done, { status: "booked", reason: null });
  assert.deepEqual(r.state().outcome, { status: "booked", reason: null });
  assert.equal(r.state().pending, false);
  assert.equal(getMeCalls(), 3);
  assert.equal(clock.pending(), 0, "no timers left once answered");
  assert.equal(seen[0], true);
  assert.equal(seen.at(-1), false);
});

test("real mode: a refusal ends pending so Book can be pressed again", async () => {
  const { r, clock } = realSetup([meWith({ request_id: "r-1", status: "refused", reason: "no_library" })]);
  const done = r.send(REQ);
  await clock.advance(POLL_MS);
  assert.deepEqual(await done, { status: "refused", reason: "no_library" });
  assert.equal(r.state().pending, false);
  const again = { ...REQ, request_id: "r-2" };
  const second = r.send(again);
  await flush();
  assert.equal(r.state().pending, true);
  assert.equal(r.state().request, again);
  assert.equal(r.state().outcome, null, "a new send clears the old outcome");
  second.catch(() => {});
});

test("a second send while one is pending is rejected and not PUT", async () => {
  const { r, puts } = realSetup([meWith()]);
  r.send(REQ);
  await flush();
  await assert.rejects(r.send({ ...REQ, request_id: "r-2" }), /pending/);
  assert.equal(puts.length, 1);
  assert.equal(r.state().request.request_id, "r-1");
});

test("a second send is rejected even before the PUT finishes", async () => {
  const clock = fakeClock();
  let release;
  const put = () => new Promise((res) => { release = res; });
  const r = createRequester({ putUrl: "u", getMe: async () => meWith(), fixture: false, now: clock.now, setTimer: clock.setTimer, put });
  r.send(REQ);
  assert.equal(r.state().pending, true, "Book is disabled from the PUT");
  await assert.rejects(r.send({ ...REQ, request_id: "r-2" }), /pending/);
  release();
});

test("a failed PUT ends pending and rejects", async () => {
  const clock = fakeClock();
  const put = async () => { throw new Error("PUT failed: HTTP 500"); };
  const r = createRequester({ putUrl: "u", getMe: async () => meWith(), fixture: false, now: clock.now, setTimer: clock.setTimer, put });
  await assert.rejects(r.send(REQ), /HTTP 500/);
  assert.equal(r.state().pending, false);
  assert.equal(clock.pending(), 0);
});

test("a failed poll keeps polling", async () => {
  const { r, clock } = realSetup([new Error("network"), meWith({ request_id: "r-1", status: "booked", reason: null })]);
  const done = r.send(REQ);
  await clock.advance(POLL_MS);
  assert.equal(r.state().pending, true);
  await clock.advance(POLL_MS);
  assert.equal((await done).status, "booked");
});

test("after 2 minutes unanswered the state says DraftBot hasn't answered", async () => {
  const { r, clock } = realSetup([meWith()]);
  let changes = 0;
  r.onChange(() => changes++);
  r.send(REQ);
  await clock.advance(SLOW_MS - 1);
  assert.equal(r.state().slow, false);
  const before = changes;
  await clock.advance(1);
  assert.equal(r.state().slow, true);
  assert.ok(changes > before, "listeners hear about the slow flag");
  assert.equal(r.state().pending, true, "still pending; it keeps polling");
});

test("fixture mode never PUTs and resolves with the fake outcome after 1.5 s", async () => {
  const clock = fakeClock();
  let putCalls = 0, getMeCalls = 0;
  const fakes = [];
  const r = createRequester({
    putUrl: null, fixture: true, now: clock.now, setTimer: clock.setTimer,
    put: async () => { putCalls++; }, getMe: async () => { getMeCalls++; return meWith(); },
    fake: (req) => { fakes.push(req); return { status: "refused", reason: "already_booked" }; },
  });
  const done = r.send(REQ);
  await clock.advance(FAKE_MS - 1);
  assert.equal(r.state().pending, true);
  await clock.advance(1);
  assert.deepEqual(await done, { status: "refused", reason: "already_booked" });
  assert.deepEqual(fakes, [REQ]);
  assert.equal(putCalls, 0);
  assert.equal(getMeCalls, 0);
  assert.equal(r.state().pending, false);
  assert.equal(FAKE_MS, 1500);
  assert.equal(POLL_MS, 5000);
  assert.equal(SLOW_MS, 120000);
});

test("onChange returns an unsubscribe", async () => {
  const clock = fakeClock();
  const r = createRequester({ fixture: true, now: clock.now, setTimer: clock.setTimer, fake: () => ({ status: "booked", reason: null }) });
  let n = 0;
  const off = r.onChange(() => n++);
  off();
  await Promise.all([r.send(REQ), clock.advance(FAKE_MS)]);
  assert.equal(n, 0);
});
