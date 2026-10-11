import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs/promises";
import { BrowserCapacity } from "../scripts/flow-ai/browser-capacity.mjs";
import { profileOnlyBrowserClass, waitForNativeBrowserExit } from "../scripts/flow-ai/profiles.mjs";
import { AiFlowWorker } from "../scripts/flow-ai/browser-agent.mjs";
import { AccountChecks } from "../scripts/flow-ai/account-pool.mjs";

const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
function child() {
  const native = new EventEmitter();
  native.exitCode = null; native.signalCode = null; native.killed = false;
  native.kill = () => { native.killed = true; };
  native.exit = () => { native.signalCode = "SIGTERM"; native.emit("exit", null, "SIGTERM"); };
  return native;
}

test("five queued profile owners share exactly one FIFO browser permit", async () => {
  const capacity = new BrowserCapacity();
  const owners = Array.from({ length: 5 }, () => ({}));
  const seen = [];
  const permits = owners.map((owner, index) => capacity.acquire(owner).then(() => seen.push(index)));
  await tick();
  assert.deepEqual(seen, [0]);
  assert.deepEqual(capacity.stats(), { limit: 1, active: 1, queued: 4, lazy_start: true, release_after_task: true });
  for (let index = 0; index < owners.length; index++) {
    assert.equal(capacity.owner, owners[index]);
    capacity.release(owners[index]);
    await tick();
  }
  await Promise.all(permits);
  assert.deepEqual(seen, [0, 1, 2, 3, 4]);
  assert.equal(capacity.stats().active, 0);
});

test("service shutdown aborts waiting workers and cannot open a Chrome while closing", async () => {
  const capacity = new BrowserCapacity(); const owner = {};
  await capacity.acquire(owner); let modelCalls = 0;
  const Browser = profileOnlyBrowserClass(class { async close() {} }, 5, capacity);
  const worker = new AiFlowWorker({ browser: new Browser(), createModel: () => { modelCalls++; throw new Error("must not start model"); } });
  const task = worker.run({ task: {} }); const rejected = assert.rejects(task, /排队任务/);
  await tick(); assert.equal(worker.queued, 1);
  capacity.stop(); capacity.release(owner); await rejected;
  assert.equal(worker.queued, 0); assert.equal(worker.active, false); assert.equal(modelCalls, 0);
  assert.equal(capacity.stats().active, 0); assert.equal(capacity.stats().queued, 0);
  assert.equal(capacity.canAcquire(owner), false);
  await assert.rejects(capacity.acquire({}), /正在停止/);
});

test("next browser waits for context close completion; failed close retains permit", async () => {
  const capacity = new BrowserCapacity();
  const closing = deferred();
  class Base { async ensurePage() {} async close() {} }
  const Browser = profileOnlyBrowserClass(Base, 5, capacity);
  const browser = new Browser();
  await browser.acquireCapacity();
  browser.context = { close: () => closing.promise };
  const other = {};
  let opened = false;
  const next = capacity.acquire(other).then(() => { opened = true; });
  const close = browser.close();
  await tick(); assert.equal(opened, false);
  closing.resolve(); await close; await next;
  assert.equal(opened, true); capacity.release(other);
  await browser.acquireCapacity();
  browser.context = { close: async () => { throw new Error("close failed"); } };
  await assert.rejects(browser.close(), /close failed/);
  assert.equal(capacity.canAcquire(other), false);
});

test("duplicate manual starts cannot release the first login's permit", async () => {
  const capacity = new BrowserCapacity(); const opening = deferred();
  class Base {
    async close() {}
    async beginInteractiveLogin() {
      this.stage = "interactive_login";
      await this.close(); await opening.promise;
      return { opened: true };
    }
  }
  const Browser = profileOnlyBrowserClass(Base, 5, capacity); const browser = new Browser();
  const first = browser.beginInteractiveLogin(); await tick();
  await assert.rejects(browser.beginInteractiveLogin(), /已经/);
  assert.equal(capacity.stats().active, 1);
  assert.equal(capacity.canAcquire({}), false);
  opening.resolve(); await first;
  assert.equal(capacity.stats().active, 1);
  browser.stage = "idle"; await browser.close();
});

test("cancel and verify require actual native exit, not merely killed=true", async () => {
  const capacity = new BrowserCapacity(); const native = child();
  let verified = 0;
  class Base {
    async close() {}
    async finishInteractiveLogin() { verified++; return { ready: true }; }
    async cancelInteractiveLogin() { this.interactiveLogin.nativeBrowser.kill(); await this.close(); this.interactiveLogin = null; }
  }
  const Browser = profileOnlyBrowserClass(Base, 5, capacity); const browser = new Browser();
  browser.interactiveLogin = { nativeBrowser: native }; await browser.acquireCapacity();
  const cancel = browser.cancelInteractiveLogin(); await tick();
  assert.equal(native.killed, true);
  await assert.rejects(browser.finishInteractiveLogin(), /先关闭|处理中/);
  assert.equal(verified, 0); assert.equal(capacity.canAcquire({}), false);
  native.exit(); await cancel;
  assert.equal(capacity.stats().active, 0);
  const stuck = child();
  await assert.rejects(waitForNativeBrowserExit(stuck, 5), /尚未实际退出/);
  assert.equal(stuck.listenerCount("exit"), 0);
});

test("model startup, task reset and lifecycle errors release browser and worker state", async () => {
  for (const failure of ["model", "task", "onStart", "modelClose"]) {
    const capacity = new BrowserCapacity();
    const Browser = profileOnlyBrowserClass(class { async close() {} async ensurePage() { throw new Error("page failure"); } }, 5, capacity);
    const worker = new AiFlowWorker({ browser: new Browser(), createModel: () => {
      if (failure === "model") throw new Error("model failure");
      return { close() { if (failure === "modelClose") throw new Error("model close failure"); } };
    } });
    const task = failure === "task" ? Object.freeze({ newProjectClicked: true }) : {};
    await assert.rejects(worker.run({ task }, { onStart() { if (failure === "onStart") throw new Error("callback failure"); } }));
    assert.equal(worker.queued, 0, failure); assert.equal(worker.active, false, failure);
    assert.equal(capacity.stats().active, 0, failure);
  }
});

test("inspect still closes browser when model.close throws", async () => {
  const capacity = new BrowserCapacity();
  const Browser = profileOnlyBrowserClass(class { async close() {} }, 5, capacity);
  const worker = new AiFlowWorker({ browser: new Browser(), createModel: () => ({ request: async () => { throw new Error("mock check stopped"); }, close() { throw new Error("mock model close failed"); } }) });
  await assert.rejects(worker.inspect(), /model close failed/);
  assert.equal(capacity.stats().active, 0);
});

test("profile wrapper serializes verify/cancel even if caller bypasses worker", async () => {
  const capacity = new BrowserCapacity(); const pending = deferred(); let opens = 0;
  class Base {
    async close() {}
    async ensurePage() { opens++; await pending.promise; }
    async finishInteractiveLogin() { await this.ensurePage(); await this.close(); return {}; }
    async cancelInteractiveLogin() { throw new Error("must not cancel during verification"); }
  }
  const Browser = profileOnlyBrowserClass(Base, 5, capacity); const browser = new Browser();
  const first = browser.finishInteractiveLogin(); await tick();
  await assert.rejects(browser.finishInteractiveLogin(), /处理中/);
  await assert.rejects(browser.cancelInteractiveLogin(), /处理中/);
  assert.equal(opens, 1); assert.equal(capacity.canAcquire({}), false);
  pending.resolve(); await first; assert.equal(capacity.stats().active, 0);
});

test("queued profiles do not start model bridges until the browser permit is available", async () => {
  const capacity = new BrowserCapacity(); const blocker = {};
  await capacity.acquire(blocker); let modelCalls = 0;
  const Browser = profileOnlyBrowserClass(class { async close() {} }, 5, capacity);
  const worker = new AiFlowWorker({ browser: new Browser(), createModel: () => { modelCalls++; throw new Error("mock stop"); } });
  const task = worker.run({ task: {} }); const rejection = assert.rejects(task, /mock stop/);
  await tick(); assert.equal(modelCalls, 0); assert.equal(worker.queued, 1); assert.equal(worker.active, false);
  capacity.release(blocker); await rejection;
  assert.equal(modelCalls, 1); assert.equal(capacity.stats().active, 0);
});

test("worker login operations serialize start, verify and cancel", async () => {
  for (const action of ["beginInteractiveLogin", "finishInteractiveLogin", "cancelInteractiveLogin"]) {
    const pending = deferred(); let calls = 0;
    const worker = new AiFlowWorker({ browser: { [action]: async () => { calls++; await pending.promise; return {}; } }, createModel: () => null });
    const first = worker[action]();
    assert.throws(() => worker.beginInteractiveLogin(), /不能关闭/);
    assert.throws(() => worker.finishInteractiveLogin(), /不能关闭/);
    assert.throws(() => worker.cancelInteractiveLogin(), /不能关闭/);
    await tick(); assert.equal(calls, 1);
    pending.resolve(); await first; assert.equal(worker.load(), 0);
  }
});

test("readiness cache is per-slot, distinguishes unchecked/busy and never launches a worker", () => {
  const workers = Array.from({ length: 5 }, () => ({ load: () => 0, inspect: () => { throw new Error("must not inspect"); } }));
  const checks = new AccountChecks(5);
  assert.equal(checks.snapshot(workers).accounts.every(item => item.checked === false), true);
  checks.record(3, { ready: true, account_email: "mock@example.test" });
  checks.record(1, { ready: false, error: "needs login" });
  checks.record(3, { ready: false, busy: true });
  workers[2].load = () => 1;
  const result = checks.snapshot(workers);
  assert.equal(result.ready, true); assert.equal(result.usableAccountCount, 1);
  assert.equal(result.accounts[2].checked, true); assert.equal(result.accounts[2].busy, true);
  assert.equal(result.accounts[2].ready, true); assert.equal(result.accounts[3].checked, false);
  checks.clear(3); assert.equal(checks.snapshot(workers).ready, false);
});

test("startup and GET health/readiness do not preload browsers or resume journal jobs", async () => {
  // Source-level guard complements lifecycle mocks without importing an API
  // that can spawn browsers. No running service or Chrome is used in this test.
  const api = await fs.readFile(new URL("../scripts/flow-ai/api.mjs", import.meta.url), "utf8");
  const healthRoutes = api.slice(api.indexOf('if (request.method === "GET" && url.pathname === "/healthz")'), api.indexOf('if (request.method === "GET" && url.pathname === "/v1/models")'));
  assert.doesNotMatch(healthRoutes, /inspect\(|ensureReady\(|ensurePage\(/);
  assert.match(healthRoutes, /accountChecks\.snapshot/);
  const startup = api.slice(api.indexOf("for (const filename of await fs.readdir(journalDir))"), api.indexOf("const server = http.createServer"));
  assert.doesNotMatch(startup, /runJob\(|inspect\(|ensurePage\(/);
  assert.match(startup, /resume_required/);
  const stop = api.slice(api.indexOf("async function stop()"));
  assert.ok(stop.indexOf("browserCapacity.stop()") < stop.indexOf("pool.close()"));
  const guard = await fs.readFile(new URL("../scripts/flow-api-guard.ps1", import.meta.url), "utf8");
  assert.doesNotMatch(guard, /Test-FlowReadiness/, "guard may never scan all profiles on startup");
});
