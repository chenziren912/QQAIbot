// Five profiles on disk do not mean five Chromium processes. The single
// permit is shared by generation, explicit checks, and manual login.
export class BrowserCapacity {
  constructor() { this.owner = null; this.waiters = []; this.stopped = false; }
  canAcquire(owner) { return !this.stopped && (this.owner === null || this.owner === owner); }
  acquire(owner) {
    if (this.stopped) return Promise.reject(Object.assign(new Error("Flow 浏览器服务正在停止，不再启动浏览器"), { code: "flow_runtime_stopping" }));
    if (this.owner === owner) return Promise.resolve();
    if (this.owner === null) { this.owner = owner; return Promise.resolve(); }
    const existing = this.waiters.find((item) => item.owner === owner);
    if (existing) return existing.promise;
    let resolve, reject;
    const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
    this.waiters.push({ owner, promise, resolve, reject });
    return promise;
  }
  release(owner) {
    if (this.owner !== owner) return;
    const next = this.waiters.shift();
    this.owner = next?.owner || null;
    next?.resolve();
  }
  stop() {
    this.stopped = true;
    const waiting = this.waiters.splice(0);
    for (const item of waiting) item.reject(Object.assign(new Error("Flow 浏览器服务停止，已取消尚未启动的排队任务"), { code: "flow_runtime_stopping" }));
  }
  stats() { return { limit: 1, active: Number(this.owner !== null), queued: this.waiters.length, lazy_start: true, release_after_task: true }; }
}
