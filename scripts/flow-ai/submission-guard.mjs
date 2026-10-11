// Stop definitely unsubmitted work before the Python create request expires.
// Once input may have reached Generate, only observation/recovery may decide
// the outcome; disconnecting its caller must never submit a replacement.
export const SUBMISSION_TIMEOUT_MS = 19 * 60 * 1000;

export function submissionMayHaveStarted(task) {
  return Boolean(task.submissionUnknown || task.submissionConfirmed || task.promptSent);
}

export class SubmissionGuard {
  constructor(task, { timeoutMs = SUBMISSION_TIMEOUT_MS, now = Date.now, schedule = setTimeout, unschedule = clearTimeout } = {}) {
    this.task = task;
    this.now = now;
    this.unschedule = unschedule;
    this.listeners = new Set();
    task.submissionDeadlineAt ||= now() + Math.min(timeoutMs, SUBMISSION_TIMEOUT_MS);
    this.timer = schedule(() => this.cancel("submission_deadline_exceeded"), Math.max(0, task.submissionDeadlineAt - now()));
    this.timer?.unref?.();
  }
  cancel(code = "submission_cancelled") {
    if (submissionMayHaveStarted(this.task) || this.error) return false;
    this.error = Object.assign(new Error(code === "submission_deadline_exceeded"
      ? "Flow 提交等待超过截止时间；尚未点击生成，已取消，不会稍后生成"
      : "Flow 创建请求已断开；尚未点击生成，已取消，不会稍后生成"), {
      code, preventAccountRetry: true, cancelledBeforeSubmission: true, submissionStarted: false,
    });
    this.task.preSubmissionCancelled = true;
    this.task.preSubmissionCancellationCode = code;
    this.unschedule(this.timer);
    for (const listener of this.listeners) listener(this.error);
    return true;
  }
  assert() {
    if (submissionMayHaveStarted(this.task)) return;
    if (this.now() >= this.task.submissionDeadlineAt) this.cancel("submission_deadline_exceeded");
    if (this.error) throw this.error;
  }
  onCancel(listener) {
    this.listeners.add(listener);
    if (this.error) listener(this.error);
    return () => this.listeners.delete(listener);
  }
  dispose() { this.unschedule(this.timer); this.listeners.clear(); }
}

export function bindSubmissionCaller(response, guard) {
  // IncomingMessage.close also fires after a normally consumed request body;
  // ServerResponse.close identifies a lost caller while the POST is waiting.
  const disconnected = () => { if (!response.writableEnded) guard.cancel(); };
  response.once("close", disconnected);
  if (response.destroyed) disconnected();
  return () => response.removeListener("close", disconnected);
}

export function refundUnsubmittedBudget(job, budget) {
  if (submissionMayHaveStarted(job) || job.creditReservationReleased) return false;
  job.creditReservationReleased = true;
  budget.committed = Math.max(0, budget.committed - job.estimatedCredits);
  return true;
}

// The external pool predates cancellation and retries every pre-submit error.
// Keep its account rotation, but treat our cancellation/deadline as terminal.
export async function runWithSubmissionGuard(pool, request, lifecycle = {}) {
  const errors = [];
  let previousIndex = null;
  for (let attempt = 1; attempt <= pool.maxAccountAttempts; attempt++) {
    request.submissionGuard?.assert();
    const index = pool.chooseWorker(previousIndex), account = index + 1;
    const details = { account, attempt, maxAttempts: pool.maxAccountAttempts, worker: index };
    lifecycle.onAttemptQueued?.(details);
    try {
      return await pool.workers[index].run(request, {
        onStart: (value) => lifecycle.onStart?.({ ...value, account, attempt }),
        onStage: (value) => lifecycle.onStage?.({ ...value, account, attempt }),
        onHeartbeat: (value) => lifecycle.onHeartbeat?.({ ...value, account, attempt }),
      });
    } catch (error) {
      Object.assign(error, { account, attempt });
      if (error.preventAccountRetry || error.submissionStarted || submissionMayHaveStarted(request.task)) throw error;
      request.submissionGuard?.assert();
      pool.lastReadiness = null;
      errors.push(`account ${account}: ${error.message}`);
      lifecycle.onRetry?.({ ...details, error: error.message });
      previousIndex = index;
      if (attempt === pool.maxAccountAttempts) throw Object.assign(new Error(`Google Flow could not start generation after ${errors.length} account attempt(s): ${errors.join(" | ")}`), { submissionStarted: false, attemptErrors: errors });
    }
  }
  throw new Error("No usable Google Flow account is configured.");
}
