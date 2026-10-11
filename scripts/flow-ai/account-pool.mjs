// Unauthenticated/new profiles must not consume all three attempts before
// reaching an already-usable account. Never select a manual login window.
export function chooseAvailableWorker(workers, roundRobin, previousIndex, readiness = []) {
  const candidates = workers.map((worker, index) => ({ index, load: worker.load(), distance: (index - roundRobin + workers.length) % workers.length }))
    .filter(({ index }) => readiness[index] !== false && workers[index].stats().stage !== "interactive_login");
  if (!candidates.length) throw new Error("Flow 没有可用的已登录账号；请在专用浏览器完成登录并重新检测");
  const preferred = candidates.filter(({ index }) => candidates.length === 1 || index !== previousIndex);
  const eligible = preferred.length ? preferred : candidates;
  const proven = eligible.filter(({ index }) => readiness[index] === true);
  return (proven.length ? proven : eligible).sort((left, right) => left.load - right.load || left.distance - right.distance)[0].index;
}

// GET health/readiness only reads this cache; only an explicit check, login
// verification or observed cloud submission can add successful evidence.
export class AccountChecks {
  constructor(count) { this.records = Array(count).fill(null); }
  record(account, result) {
    if (!result.busy) this.records[account - 1] = { ...result, ready: result.ready === true, checked: true, checked_at: Date.now() };
  }
  clear(account) { this.records[account - 1] = null; }
  snapshot(workers) {
    const accounts = this.records.map((record, index) => ({
      account: index + 1,
      ...(record || { ready: false, checked: false, note: "尚未显式检测；健康查询不会打开 Chrome" }),
      busy: workers[index].load() > 0,
    }));
    const usable = accounts.filter((item) => item.ready).length;
    return { ready: usable > 0, fullConcurrencyReady: usable === accounts.length, usableAccountCount: usable, accountCount: accounts.length, accounts, checkedAt: Math.max(0, ...accounts.map((item) => item.checked_at || 0)) };
  }
}
