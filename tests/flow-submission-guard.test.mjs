import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { AiFlowWorker } from "../scripts/flow-ai/browser-agent.mjs";
import { BrowserCapacity } from "../scripts/flow-ai/browser-capacity.mjs";
import { SubmissionGuard, SUBMISSION_TIMEOUT_MS, bindSubmissionCaller, refundUnsubmittedBudget, runWithSubmissionGuard } from "../scripts/flow-ai/submission-guard.mjs";

function guardFor(task, clock = { now: 1000 }) {
  let timer;
  const guard = new SubmissionGuard(task, { now: () => clock.now, schedule: (callback) => { timer = callback; return 1; }, unschedule: () => {} });
  return { guard, clock, expire: () => { clock.now = task.submissionDeadlineAt; timer(); } };
}

test("deadline precedes the Python 20 minute timeout and includes queued time", async () => {
  assert.ok(SUBMISSION_TIMEOUT_MS < 20 * 60 * 1000);
  const task = { id: "queued", estimatedCredits: 10 }, { guard, expire } = guardFor(task);
  const capacity = new BrowserCapacity(), blocker = {}, owner = {};
  await capacity.acquire(blocker);
  const budget = { committed: 10 };
  guard.onCancel(() => { task.status = "failed"; refundUnsubmittedBudget(task, budget); });
  let modelStarts = 0, browserStarts = 0, closed = 0;
  const worker = new AiFlowWorker({ browser: {
    acquireCapacity: () => capacity.acquire(owner),
    ensurePage: () => { browserStarts++; assert.fail("expired work cannot open a browser"); },
    close: async () => { closed++; capacity.release(owner); },
  }, createModel: () => { modelStarts++; assert.fail("expired work cannot start a model"); } });
  const running = worker.run({ task, submissionGuard: guard });
  const rejected = assert.rejects(running, (error) => error.code === "submission_deadline_exceeded" && !error.submissionStarted && error.preventAccountRetry);
  await new Promise((resolve) => setImmediate(resolve));
  expire();
  assert.equal(task.status, "failed", "parent/public status is terminal before the occupied permit is released");
  assert.equal(budget.committed, 0);
  capacity.release(blocker);
  await rejected;
  assert.equal(modelStarts + browserStarts, 0);
  assert.equal(closed, 1);
  assert.equal(worker.queued, 0);
  assert.equal(worker.active, false);
  assert.equal(capacity.stats().active, 0);
});

test("lost callers cancel only definitely unsubmitted work and normal HTTP close is harmless", () => {
  for (const state of [{}, { submissionUnknown: true }, { submissionConfirmed: true }, { promptSent: true }]) {
    const task = { ...state }, { guard, expire } = guardFor(task);
    const response = new EventEmitter(); response.writableEnded = false;
    const detach = bindSubmissionCaller(response, guard);
    response.emit("close");
    if (!Object.keys(state).length) assert.throws(() => guard.assert(), (error) => error.code === "submission_cancelled");
    else { expire(); assert.doesNotThrow(() => guard.assert()); assert.equal(task.preSubmissionCancelled, undefined); }
    detach(); assert.equal(response.listenerCount("close"), 0); guard.dispose();
  }
  const { guard } = guardFor({});
  const response = new EventEmitter(); response.writableEnded = true;
  bindSubmissionCaller(response, guard); response.emit("close");
  assert.doesNotThrow(() => guard.assert()); guard.dispose();
});

test("cancelled queued work cannot rotate accounts and a normal pre-submit failure still rotates", async () => {
  let selections = 0, attempts = 0;
  const task = {}, { guard } = guardFor(task);
  const pool = { maxAccountAttempts: 3, chooseWorker: () => { selections++; return 0; }, workers: [{
    run: async () => { attempts++; guard.cancel(); guard.assert(); },
  }] };
  await assert.rejects(runWithSubmissionGuard(pool, { task, submissionGuard: guard }), (error) => error.preventAccountRetry);
  assert.equal(selections, 1); assert.equal(attempts, 1); guard.dispose();
  selections = 0;
  const normalPool = { maxAccountAttempts: 3, chooseWorker: () => selections++, workers: [
    { run: async () => { throw new Error("not logged in"); } }, { run: async () => "second account result" },
  ] };
  assert.equal(await runWithSubmissionGuard(normalPool, { task: {} }), "second account result");
  assert.equal(selections, 2);
});

test("disconnect before binding or while the initial journal awaits cannot start a worker", async () => {
  for (const alreadyDisconnected of [true, false]) {
    const task = {}, { guard } = guardFor(task);
    const response = new EventEmitter(); response.writableEnded = false; response.destroyed = alreadyDisconnected;
    const detach = bindSubmissionCaller(response, guard);
    let cancelled = 0, starts = 0;
    guard.onCancel(() => cancelled++);
    let finishJournal;
    const journal = new Promise((resolve) => { finishJournal = resolve; });
    const pool = { maxAccountAttempts: 3, chooseWorker: () => { starts++; return 0; }, workers: [] };
    const pending = (async () => { await journal; return runWithSubmissionGuard(pool, { task, submissionGuard: guard }); })();
    const rejection = assert.rejects(pending, (error) => error.code === "submission_cancelled");
    if (!alreadyDisconnected) { response.destroyed = true; response.emit("close"); }
    finishJournal(); await rejection;
    assert.equal(cancelled, 1); assert.equal(starts, 0);
    detach(); guard.dispose();
  }
});

test("cancellation and eventual worker rejection refund exactly once; uncertain jobs never refund", () => {
  const budget = { committed: 20 }, task = { estimatedCredits: 10 };
  assert.equal(refundUnsubmittedBudget(task, budget), true);
  assert.equal(refundUnsubmittedBudget(task, budget), false);
  assert.equal(budget.committed, 10);
  for (const state of [{ submissionUnknown: true }, { submissionConfirmed: true }, { promptSent: true }]) {
    assert.equal(refundUnsubmittedBudget({ estimatedCredits: 10, ...state }, budget), false);
    assert.equal(budget.committed, 10);
  }
});

for (const boundary of ["model_reply", "fresh_validation", "caller_close"]) {
  test(`no paid input after cancellation/deadline at ${boundary}`, async () => {
    const url = "https://flow.google.com/project/new", prompt = "A tiny cat waves";
    let pageUrl = "https://flow.google.com/", decisions = 0, paidClicks = 0, browserCloses = 0;
    const task = { id: "late_action" }, { guard, clock } = guardFor(task);
    const elements = [
      { id: 0, tag: "button", label: "Video · Omni 1.1 Flash · 8s · 16:9 · x1", token: "settings" },
      { id: 1, role: "textbox", label: "", value: prompt, token: "prompt" },
      { id: 2, tag: "button", label: "arrow_forward Create", token: "submit" },
      { id: 3, tag: "button", label: "New project", token: "new" },
    ];
    const page = { url: () => pageUrl, setViewportSize: async () => {}, evaluate: async () => [], locator: (selector) => {
      const item = elements.find((element) => selector.includes(`"${element.token}"`));
      return { isVisible: async () => true, evaluate: async (callback) => callback({ innerText: item.label, getAttribute: (name) => name === "aria-label" ? item.label : null }),
        click: async () => { if (item.token === "new") pageUrl = url; else if (item.token === "submit") paidClicks++; } };
    } };
    const worker = new AiFlowWorker({ browser: { ensurePage: async () => page, assertUsableSession: async () => {}, snapshotMedia: async () => new Set(), close: async () => { browserCloses++; } },
      createModel: () => ({ close() {}, request: async () => {
        if (++decisions === 1) return { decision: { action: "click", element: 3 } };
        if (boundary === "model_reply") clock.now = task.submissionDeadlineAt;
        if (boundary === "caller_close") guard.cancel();
        return { decision: { action: "submit", element: 2 } };
      } }), observePage: async (_page, options) => {
        if (options?.screenshot === false && boundary === "fresh_validation") clock.now = task.submissionDeadlineAt;
        return { text: "Video", url: pageUrl, elements };
      }, sleep: async () => assert.fail("cancelled submission must not wait for another decision") });
    await assert.rejects(worker.run({ task, submissionGuard: guard, prompt, duration: 8, aspectRatio: "16:9", outputs: 1 }), (error) => error.preventAccountRetry && !error.submissionStarted);
    assert.equal(paidClicks, 0); assert.equal(decisions, 2); assert.equal(browserCloses, 1);
    assert.equal(worker.active, false); assert.equal(worker.queued, 0); guard.dispose();
  });
}

test("worker.run always returns a rejected promise even for invalid frozen task state", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => assert.fail("must not open a model") });
  await assert.rejects(worker.run({ task: Object.freeze({ id: "frozen" }) }), /extensible/);
  assert.equal(worker.queued, 0); assert.equal(worker.active, false);
});
