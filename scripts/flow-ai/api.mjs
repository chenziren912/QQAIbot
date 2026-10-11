/** QQBot-owned AI Flow runtime. Existing Flow library is used for session
 * storage and DOM evidence only; all workflow actions come from the Bot LLM. */
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import http from "node:http";
import { pathToFileURL, fileURLToPath } from "node:url";
import { AiFlowWorker, BotModel, REVIEW_INTERVAL_MS, canRecoverSubmission } from "./browser-agent.mjs";
import { configureProfileEnvironment, applyProfileConfiguration, profileOnlyBrowserClass } from "./profiles.mjs";
import { chooseAvailableWorker, AccountChecks } from "./account-pool.mjs";
import { BrowserCapacity } from "./browser-capacity.mjs";
import { SubmissionGuard, SUBMISSION_TIMEOUT_MS, bindSubmissionCaller, refundUnsubmittedBudget, runWithSubmissionGuard } from "./submission-guard.mjs";
import { flowRequestPolicy } from "./request-policy.mjs";

const root = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const project = path.resolve(process.env.FLOW_VIDEO_PROJECT_DIR || "");
if (!process.env.FLOW_VIDEO_PROJECT_DIR) throw new Error("FLOW_VIDEO_PROJECT_DIR must point to the Flow session library");
const profiles = configureProfileEnvironment();
const { config } = await import(pathToFileURL(path.join(project, "src/config.js")));
applyProfileConfiguration(config, profiles);
const { FlowBrowser } = await import(pathToFileURL(path.join(project, "src/flow-browser.js")));
const browserCapacity = new BrowserCapacity();
const ProfileOnlyFlowBrowser = profileOnlyBrowserClass(FlowBrowser, profiles.dirs.length, browserCapacity);
const { FlowBrowserPool } = await import(pathToFileURL(path.join(project, "src/flow-browser-pool.js")));
const { estimateCredits } = await import(pathToFileURL(path.join(project, "src/credits.js")));
const dataDir = path.resolve(process.env.QQ_AI_DATA_DIR || path.join(process.env.LOCALAPPDATA || root, "QQAIGroupAgent/data"));
const journalDir = path.join(dataDir, "flow-ai-jobs");
await fs.mkdir(journalDir, { recursive: true });
const jobs = new Map();
const snapshots = new Map();
const saving = new Map();
const MODEL = "gemini-omni-flash";
const secondsNow = () => Math.floor(Date.now() / 1000);
const budget = { committed: 0 };
const createModel = () => new BotModel({ python: process.env.FLOW_AI_PYTHON || path.join(root, ".venv/Scripts/python.exe"), script: path.join(root, "scripts/flow-ai-model.py"), dataDir });
let stopping = false;

function safeError(error) {
  // Browser/model helpers are responsible for credential redaction; retain
  // useful diagnosis without emitting headers, cookies or full requests.
  return String(error?.message || error).replace(/[\r\n]+/g, " ").slice(0, 1400);
}
function persist(job) {
  const serialized = JSON.stringify(job);
  const prior = saving.get(job.id) || Promise.resolve();
  const next = prior.catch(() => {}).then(async () => {
    const destination = path.join(journalDir, `${job.id}.json`);
    const temporary = destination + ".tmp";
    await fs.writeFile(temporary, serialized);
    await fs.rename(temporary, destination);
  });
  saving.set(job.id, next);
  next.catch((error) => console.error(JSON.stringify({ event: "journal_error", job_id: job.id, error: safeError(error) })));
  return next;
}
function emit(job, event, details = {}) {
  if (!job) return;
  if (event === "observation") {
    snapshots.set(job.id, details.screenshot);
    // Keep only the latest real screenshot. No images accumulate per tick.
    const bytes = Buffer.from(details.screenshot.split(",")[1], "base64");
    void fs.writeFile(path.join(journalDir, `${job.id}.jpg`), bytes).catch(() => {});
    job.lastObservation = { text: details.text, url: details.url, observed_at: job.lastObservedAt, page_id: details.pageId,
      elements: (details.elements || []).map(({ id, tag, role, label, inner_text, title, selected, has_popup, controls }) => ({ id, tag, role, label, inner_text, title, selected, has_popup, controls })) };
  } else {
    const item = { at: secondsNow(), event, ...details };
    job.trace ||= [];
    job.trace.push(item);
    job.trace = job.trace.slice(-40);
    console.error(JSON.stringify({ job_id: job.id, account: job.account, ...item }));
  }
  void persist(job);
}
const pool = new FlowBrowserPool({
  workerFactory: (accountIndex) => new AiFlowWorker({ browser: new ProfileOnlyFlowBrowser({ accountIndex }), createModel, timeoutMs: config.timeoutMs, emit }),
});
pool.accountReadiness = [];
const accountChecks = new AccountChecks(pool.workers.length);
function cacheAccountCheck(account, result) {
  accountChecks.record(account, result);
  if (!result.busy) pool.accountReadiness[account - 1] = result.ready === true;
}
pool.readinessSnapshot = () => {
  const cached = accountChecks.snapshot(pool.workers);
  return { generation_ready: cached.ready, full_concurrency_ready: false, usable_account_count: cached.usableAccountCount, session_account_count: cached.accountCount, checked_at: cached.checkedAt ? Math.floor(cached.checkedAt / 1000) : null, concurrent_browser_limit: 1 };
};
pool.chooseWorker = (previousIndex = null) => {
  const index = chooseAvailableWorker(pool.workers, pool.roundRobin, previousIndex, pool.accountReadiness);
  pool.roundRobin = (index + 1) % pool.workers.length;
  return index;
};
// Readiness must not navigate a page owned by an active AI review task.
pool.inspectAll = async () => {
  const accounts = [];
  for (const [index, worker] of pool.workers.entries()) {
    const result = await worker.inspect();
    cacheAccountCheck(index + 1, result);
    accounts.push({ account: index + 1, ...result });
  }
  const usable = accounts.filter((item) => item.ready).length;
  pool.accountReadiness = accounts.map((item, index) => item.ready ? true : item.busy ? pool.accountReadiness[index] : false);
  const result = { ready: usable > 0, fullConcurrencyReady: usable === accounts.length, usableAccountCount: usable, accountCount: accounts.length, accounts, checkedAt: Date.now() };
  pool.lastReadiness = result;
  return result;
};

export function publicVideo(job) {
  return {
    id: job.id, object: "video", model: MODEL, status: job.status,
    prompt: job.prompt,
    submission_confirmed: job.submissionConfirmed === true,
    submission_unknown: job.submissionUnknown === true,
    stage: job.stage, progress: job.progress || 0, progress_observed: job.progressObserved === true,
    account: job.account || null, attempt: job.attempt || 0,
    created_at: job.createdAt, started_at: job.startedAt || null, completed_at: job.completedAt || null,
    last_heartbeat_at: job.lastHeartbeatAt || null,
    seconds: String(job.duration), size: job.size, output_count: job.outputPaths?.length || 0,
    project_url: job.projectUrl || null, error: job.error || null,
    browser_task: { id: job.submitTaskId || null, status: job.submissionConfirmed ? "completed" : job.status === "failed" ? "failed" : "running", parent_job_id: job.id },
    review_task: { id: job.reviewTaskId || null, status: job.submissionConfirmed ? job.status === "completed" ? "completed" : job.status === "failed" ? "failed" : "running" : "pending", interval_seconds: REVIEW_INTERVAL_MS / 1000, action_execution: "immediate", page_id: job.pageId || null, observation_count: job.observationCount || 0, last_observed_at: job.lastObservedAt || null },
    last_observation: job.lastObservation || null,
    recent_actions: (job.trace || []).filter((item) => item.event === "ai_action").slice(-4).map(({ phase, action, reason, aiModel, at }) => ({ phase, action, reason, ai_model: aiModel, at })),
  };
}
function signal() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function runJob(job, accepted, resume = false, recover = false, submissionGuard = null) {
  try {
    // A recovered task is pinned to its original account and cloud project.
    // A new task lets the pool rotate only definitely pre-submit failures.
    const lifecycle = {
      onAttemptQueued(details) { job.account = details.account; job.attempt = details.attempt; emit(job, "attempt_queued"); },
      onStart(details) { job.stage = details.stage; job.lastHeartbeatAt = secondsNow(); emit(job, "worker_started"); },
      onStage(details) {
        job.stage = details.stage;
        if (details.projectUrl) job.projectUrl = details.projectUrl;
        if (details.submissionStarted && !job.submissionConfirmed) {
          job.submissionConfirmed = true; job.status = "in_progress"; job.startedAt = secondsNow();
          cacheAccountCheck(job.account, { ready: true, url: job.projectUrl, evidence: "confirmed_submission" });
          // Journal the evidence before releasing the parent HTTP request.
          void persist(job).then(() => accepted.resolve(job), accepted.reject);
          emit(job, "submission_confirmed", { evidence: details.submissionEvidence, page_id: job.pageId });
        }
        job.lastHeartbeatAt = secondsNow();
      },
      onHeartbeat(details) {
        job.stage = details.stage;
        job.lastHeartbeatAt = secondsNow();
        if (Number.isFinite(details.progress)) { job.progress = details.progress; job.progressObserved = true; }
      },
      onRetry(details) { emit(job, "pre_submit_account_retry", { error: details.error }); },
    };
    const request = { task: job, resume, recover, submissionGuard, model: MODEL, duration: job.duration, aspectRatio: job.aspectRatio, outputs: job.outputs, prompt: job.prompt, firstFramePath: job.firstFramePath, outputDir: job.outputDir };
    const result = resume ? await pool.accountWorker(job.account).worker.run(request, lifecycle) : await runWithSubmissionGuard(pool, request, lifecycle);
    job.outputPaths = result.outputPaths;
    job.projectUrl = result.projectUrl;
    job.status = "completed"; job.stage = "completed"; job.progress = 100;
    job.completedAt = secondsNow(); emit(job, "completed", { outputs: job.outputPaths.length });
  } catch (error) {
    job.status = "failed"; job.stage = error.cancelledBeforeSubmission ? "cancelled_before_submission" : error.code === "flow_timeout" ? "timed_out" : "failed";
    job.completedAt = secondsNow();
    job.error = { code: error.code || "flow_browser_task_failed", message: safeError(error), type: "server_error" };
    refundUnsubmittedBudget(job, budget);
    if (!job.submissionConfirmed) accepted.reject(error);
    emit(job, "failed", { error: job.error.message, submission_unknown: job.submissionUnknown === true });
  } finally { submissionGuard?.dispose(); await persist(job); }
}
function authorized(request) {
  if (!config.apiKey) return true;
  const actual = Buffer.from(String(request.headers.authorization || "").replace(/^Bearer\s+/i, ""));
  const expected = Buffer.from(config.apiKey);
  return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
}
function json(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(value));
}
function errorResponse(response, status, code, message, extras = {}) {
  json(response, status, { error: { code, message, type: "server_error" }, ...extras });
}
async function body(request) {
  let size = 0; const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > config.apiMaxBodyBytes) throw new Error("Flow request exceeds body limit");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}
function safeWorkspace(value) {
  const fallback = path.join(dataDir, "flow-output");
  if (!value) return fallback;
  const target = path.resolve(String(value));
  const workspaceRoot = path.resolve(process.env.QQ_AI_WORKSPACE_ROOT || "D:/Workspace");
  const relative = path.relative(workspaceRoot, target);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Flow output directory must be a Bot conversation workspace");
  return target;
}
function normalize(fields) {
  const model = String(fields.model || MODEL).toLowerCase();
  if (![MODEL, "gemini-omni-flash-1.1", "omni-1.1-flash"].includes(model)) throw new Error("Flow 只允许 Gemini Omni Flash 1.1，不允许其他模型");
  const prompt = String(fields.prompt || "").trim();
  if (!prompt || prompt.length > 4000) throw new Error("prompt must contain 1..4000 characters");
  const duration = Number(fields.seconds || 8), outputs = Number(fields.n || 1);
  if (![4, 6, 8, 10].includes(duration) || !Number.isInteger(outputs) || outputs < 1 || outputs > 4) throw new Error("Unsupported seconds/output count");
  const size = String(fields.size || "1280x720");
  const portrait = new Set(["720x1280", "1024x1792", "1080x1920", "9:16"]);
  if (!portrait.has(size) && !["1280x720", "1792x1024", "1920x1080", "16:9"].includes(size)) throw new Error("Unsupported video size");
  return { prompt, duration, outputs, size, aspectRatio: portrait.has(size) ? "9:16" : "16:9", outputDir: safeWorkspace(fields.workspace_dir), firstFramePath: fields.input_reference || "", maxCredits: fields.max_credits === undefined ? config.maxCredits : Number(fields.max_credits) };
}
function health() {
  return { object: "health", ok: true, runtime: "qqbot-ai-browser", runtime_version: 5, action_execution: "immediate", browser_resources: browserCapacity.stats(), authentication_mode: "persistent_browser_profile", cookie_import_enabled: false, storage_state_import_enabled: false, ai_browser_control: true, submission_confirmation: "flow_page_dom", review_interval_seconds: REVIEW_INTERVAL_MS / 1000, poll_ms: REVIEW_INTERVAL_MS, session_configured: config.sessionAccountCount > 0, session_account_count: config.sessionAccountCount, persistent_profiles: config.persistentProfiles, timeout_ms: config.timeoutMs, timeout_seconds: config.timeoutMs / 1000, pool: pool.stats(), readiness: pool.readinessSnapshot(), budget: { configuredCredits: config.maxCredits, committedCredits: budget.committed, availableCredits: Math.max(0, config.maxCredits - budget.committed) } };
}
async function route(request, response) {
  const rejected = flowRequestPolicy(request);
  if (rejected) return errorResponse(response, rejected.status, rejected.code, rejected.message);
  if (!authorized(request)) return errorResponse(response, 401, "unauthorized", "Invalid Flow API key");
  const url = new URL(request.url, "http://127.0.0.1");
  if (request.method === "GET" && url.pathname === "/healthz") return json(response, 200, health());
  if (request.method === "GET" && url.pathname === "/readyz") {
    const cached = accountChecks.snapshot(pool.workers);
    return json(response, 200, { ...health(), readiness_scope: "cached_only_no_browser", generation_ready: cached.ready, full_concurrency_ready: false, all_profiles_checked_ready: cached.fullConcurrencyReady, usable_account_count: cached.usableAccountCount, accounts: cached.accounts });
  }
  if (request.method === "GET" && url.pathname === "/v1/models") return json(response, 200, { object: "list", data: [{ id: MODEL, object: "model", owned_by: "google-flow" }] });
  if (request.method === "GET" && url.pathname === "/v1/videos") {
    const status = url.searchParams.get("status");
    return json(response, 200, { object: "list", data: [...jobs.values()].filter((job) => !status || job.status === status).sort((a, b) => b.createdAt - a.createdAt).slice(0, Math.max(1, Math.min(100, Number(url.searchParams.get("limit") || 20)))).map(publicVideo) });
  }
  if (request.method === "POST" && url.pathname === "/v1/videos") {
    const submissionDeadlineAt = Date.now() + SUBMISSION_TIMEOUT_MS;
    if (stopping) return errorResponse(response, 503, "stopping", "Flow runtime is stopping");
    let normalized;
    try { normalized = normalize(await body(request)); }
    catch (error) { return errorResponse(response, 400, "invalid_parameters", safeError(error)); }
    const estimatedCredits = estimateCredits({ model: MODEL, duration: normalized.duration, outputs: normalized.outputs, plan: config.plan });
    if (!Number.isFinite(normalized.maxCredits) || estimatedCredits > normalized.maxCredits || budget.committed + estimatedCredits > config.maxCredits) return errorResponse(response, 429, "credit_budget_exceeded", "Flow configured credit budget exceeded");
    // A requested generation checks its chosen profile in runJob. Never scan
    // or preload every configured profile before accepting a task.
    const job = { id: `video_${Date.now().toString(36)}_${crypto.randomBytes(5).toString("hex")}`, status: "queued", submissionConfirmed: false, submissionDeadlineAt, stage: "queued", createdAt: secondsNow(), lastHeartbeatAt: secondsNow(), ...normalized, estimatedCredits, outputPaths: [], trace: [] };
    jobs.set(job.id, job); budget.committed += estimatedCredits;
    const accepted = signal();
    // Attach rejection handling before a disconnected caller or deadline can
    // cancel the job while its initial journal is being written.
    accepted.promise.catch(() => {});
    const submissionGuard = new SubmissionGuard(job);
    const detachCaller = bindSubmissionCaller(response, submissionGuard);
    submissionGuard.onCancel((error) => {
      job.status = "failed"; job.stage = "cancelled_before_submission"; job.completedAt = secondsNow();
      job.error = { code: error.code, message: safeError(error), type: "server_error" };
      refundUnsubmittedBudget(job, budget);
      emit(job, "cancelled_before_submission", { error: job.error.message });
      accepted.reject(error);
    });
    let workerStarted = false;
    try {
      await persist(job);
      if (response.destroyed) submissionGuard.cancel();
      workerStarted = true;
      void runJob(job, accepted, false, false, submissionGuard);
      await accepted.promise;
      return json(response, 200, publicVideo(job));
    }
    catch (error) {
      if (!workerStarted) { submissionGuard.dispose(); refundUnsubmittedBudget(job, budget); }
      return errorResponse(response, 502, error.code || "flow_submission_failed", safeError(error), { video: publicVideo(job) });
    }
    finally { detachCaller(); }
  }
  const resumeMatch = /^\/v1\/videos\/(video_[A-Za-z0-9_]+)\/resume-review$/.exec(url.pathname);
  if (request.method === "POST" && resumeMatch) {
    const job = jobs.get(resumeMatch[1]);
    const recover = job && !job.submissionConfirmed && canRecoverSubmission(job);
    if (!job || !(job.submissionConfirmed || recover) || !job.projectUrl || !job.account) return errorResponse(response, 409, "review_not_resumable", "Only a confirmed video or verified direct-generation click can resume observation; this never submits generation");
    if (job.status !== "failed") return errorResponse(response, 409, "review_already_active", "Review is already running or completed");
    job.status = recover ? "queued" : "in_progress"; job.stage = recover ? "ai_confirming_recovery" : "ai_review_resuming"; job.error = null;
    await persist(job);
    const accepted = signal(); accepted.promise.catch(() => {});
    void runJob(job, accepted, true, recover);
    return json(response, 200, publicVideo(job));
  }
  const match = /^\/v1\/videos\/(video_[A-Za-z0-9_]+)(?:\/(content|observation))?$/.exec(url.pathname);
  if (request.method === "GET" && match) {
    const job = jobs.get(match[1]);
    if (!job) return errorResponse(response, 404, "video_not_found", "The requested video does not exist");
    if (!match[2]) return json(response, 200, publicVideo(job));
    if (match[2] === "observation") {
      let screenshot = snapshots.get(job.id);
      if (!screenshot) { try { screenshot = "data:image/jpeg;base64," + (await fs.readFile(path.join(journalDir, `${job.id}.jpg`))).toString("base64"); } catch {} }
      return json(response, 200, { ...publicVideo(job), screenshot });
    }
    if (job.status !== "completed") return errorResponse(response, 409, "video_not_ready", "Video is not completed");
    const index = Number(url.searchParams.get("index") || 1) - 1;
    const filename = job.outputPaths[index];
    if (!filename) return errorResponse(response, 404, "output_not_found", "Output not found");
    const bytes = await fs.readFile(filename);
    response.writeHead(200, { "Content-Type": "video/mp4", "Content-Length": bytes.length, "Cache-Control": "no-store" }); response.end(bytes); return;
  }
  if (request.method === "POST" && /^\/v1\/flow\/accounts\/\d+\/login(?:\/(verify|cancel))?$/.test(url.pathname)) {
    const [, account, action = "start"] = /\/accounts\/(\d+)\/login(?:\/(\w+))?$/.exec(url.pathname);
    const result = action === "start" ? await pool.beginInteractiveLogin(account) : action === "verify" ? await pool.finishInteractiveLogin(account) : await pool.cancelInteractiveLogin(account);
    if (action === "verify") cacheAccountCheck(Number(account), result);
    if (action === "start") { accountChecks.clear(Number(account)); pool.accountReadiness[Number(account) - 1] = undefined; }
    return json(response, 200, result);
  }
  const checkMatch = /^\/v1\/flow\/accounts\/(\d+)\/check$/.exec(url.pathname);
  if (request.method === "POST" && checkMatch) {
    const { account, worker } = pool.accountWorker(checkMatch[1]);
    const result = await worker.inspect();
    cacheAccountCheck(account, result);
    return json(response, result.ready ? 200 : 409, { account, ...result });
  }
  return errorResponse(response, 404, "not_found", "Unknown Flow endpoint");
}

// Journaled, confirmed tasks require explicit resume on their original account.
// A process dying between click and confirmation is kept ambiguous and is
// never regenerated on startup.
for (const filename of await fs.readdir(journalDir)) {
  if (!/^video_[A-Za-z0-9_]+\.json$/.test(filename)) continue;
  try {
    const job = JSON.parse(await fs.readFile(path.join(journalDir, filename), "utf8"));
    if (job.estimatedCredits && (job.submissionConfirmed || job.submissionUnknown || job.status !== "failed")) budget.committed += job.estimatedCredits;
    jobs.set(job.id, job);
    if (["queued", "in_progress"].includes(job.status)) {
      if (job.submissionConfirmed && job.projectUrl && job.account) {
        job.status = "failed"; job.stage = "resume_required";
        job.error = { code: "resume_required", message: "Flow 重启后不会自动打开浏览器；原任务已确认生成，可手动恢复原项目审查，不重复生成" };
        await persist(job);
      } else {
        job.status = "failed"; job.stage = "interrupted";
        job.error = { code: "submission_interrupted", message: "Flow 提交子任务被中断，未重新点击生成；请核对项目页面的真实任务" };
        await persist(job);
      }
    }
  } catch (error) { console.error(JSON.stringify({ event: "journal_read_error", file: filename, error: safeError(error) })); }
}
const server = http.createServer((request, response) => { void route(request, response).catch((error) => errorResponse(response, 500, "flow_runtime_error", safeError(error))); });
server.requestTimeout = 0;
server.listen(config.apiPort, "127.0.0.1", () => console.error(`QQBot AI Flow browser API listening on http://127.0.0.1:${config.apiPort}; review=5s; actions=immediate`));
async function stop() {
  if (stopping) return;
  stopping = true;
  browserCapacity.stop();
  server.close();
  await Promise.allSettled([...saving.values()]);
  await pool.close();
  process.exit(0);
}
process.on("SIGTERM", stop); process.on("SIGINT", stop);
