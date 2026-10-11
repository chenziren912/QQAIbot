import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { SubmissionGuard } from "./submission-guard.mjs";

export const REVIEW_INTERVAL_MS = 5000;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const tag = (message, details = {}) => Object.assign(new Error(message), details);

export class BotModel {
  constructor({ python, script, dataDir }) {
    this.process = spawn(python, ["-u", script, "--data-dir", dataDir], { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    this.pending = [];
    createInterface({ input: this.process.stdout }).on("line", (line) => {
      const waiter = this.pending.shift();
      if (!waiter) return;
      clearTimeout(waiter.timer);
      try {
        const result = JSON.parse(line);
        if (!result.ok) waiter.reject(new Error(result.error || "Flow AI request failed"));
        else waiter.resolve({ ...result, latency_ms: Date.now() - waiter.startedAt });
      } catch (error) { waiter.reject(error); }
    });
    // stderr can contain provider diagnostics. Keep it private and bounded.
    this.process.stderr.on("data", () => {});
    const fail = (error = tag("Flow AI model bridge exited", { code: "model_bridge_closed" })) => {
      this.closed = true;
      for (const waiter of this.pending.splice(0)) { clearTimeout(waiter.timer); waiter.reject(error); }
    };
    this.process.on("error", () => fail());
    this.process.on("exit", () => fail());
    this.process.stdin.on("error", () => fail());
    this.fail = fail;
  }
  request(value) {
    if (this.closed) return Promise.reject(tag("Flow AI model bridge is closed", { code: "model_bridge_closed" }));
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject, startedAt: Date.now() };
      waiter.timer = setTimeout(() => {
        this.fail(tag("Flow AI observation request timed out", { code: "model_bridge_timeout" }));
        this.process.kill();
      }, 180_000);
      this.pending.push(waiter);
      this.process.stdin.write(JSON.stringify(value) + "\n");
    });
  }
  close() { if (this.pending.length) this.process.kill(); else this.process.stdin.end(); }
}

export async function observe(page, { screenshot: includeScreenshot = true } = {}) {
  if (page.isClosed()) throw tag("Flow 标签页已关闭，无法继续审查", { code: "browser_closed" });
  // Material menus animate after a click. Let the DOM settle before pairing
  // element metadata with the screenshot, rather than missing a visible menu.
  await pause(250);
  const state = await page.evaluate(() => {
    const visible = (node) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const nodes = [...document.querySelectorAll('button,a,input,textarea,select,[contenteditable="true"],[role="button"],[role="menuitem"],[role="option"],[role="radio"],[role="tab"],[role="switch"],[role="combobox"],[aria-haspopup],[aria-controls],video,img')].filter(visible);
    const elements = nodes.slice(0, 180).map((node, index) => {
      const token = node.getAttribute("data-flow-agent") || `${Math.random().toString(36).slice(2)}-${index}`;
      node.setAttribute("data-flow-agent", token);
      const box = node.getBoundingClientRect();
      let settingSection = null;
      for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const text = parent.innerText || "";
        const video = /视频生成默认设置|Video generation defaults/i.test(text);
        const image = /图片生成默认设置|Image generation defaults/i.test(text);
        if (video && image) break;
        if (video || image) { settingSection = video ? "video" : "image"; break; }
      }
      return {
        id: index, token, tag: node.tagName.toLowerCase(), role: node.getAttribute("role"),
        label: (node.getAttribute("aria-label") || node.getAttribute("alt") || node.innerText || node.getAttribute("placeholder") || "").trim().slice(0, 500),
        inner_text: (node.innerText || "").trim().slice(0, 500),
        title: node.getAttribute("title"),
        has_popup: node.getAttribute("aria-haspopup"),
        controls: node.getAttribute("aria-controls"),
        card_text: (node.closest('[role="dialog"],article,[data-testid*="card"]')?.innerText || node.parentElement?.parentElement?.innerText || "").trim().slice(0, 2000),
        setting_section: settingSection,
        value: /^(INPUT|TEXTAREA)$/.test(node.tagName) ? String(node.value).slice(0, 4000) : node.isContentEditable ? node.innerText.slice(0, 4000) : undefined,
        editable: node.isContentEditable || /^(INPUT|TEXTAREA)$/.test(node.tagName),
        disabled: Boolean(node.disabled) || node.getAttribute("aria-disabled") === "true",
        src: /^(IMG|VIDEO)$/.test(node.tagName) ? (node.currentSrc || node.getAttribute("src") || node.getAttribute("data-src")) : undefined,
        selected: node.getAttribute("aria-selected") || node.getAttribute("aria-checked") || node.getAttribute("aria-pressed") || (node.getAttribute("data-state") === "active" || node.getAttribute("data-state") === "checked" ? "true" : null),
        expanded: node.getAttribute("aria-expanded"),
        rect: { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) },
      };
    });
    return { text: document.body.innerText.slice(0, 22000), elements, url: location.href };
  });
  if (!includeScreenshot) return state;
  const screenshot = await page.screenshot({ type: "jpeg", quality: 60, timeout: 15000 });
  return { ...state, screenshot: `data:image/jpeg;base64,${screenshot.toString("base64")}` };
}

export function validateSubmit(request, observation, action, freshProjectUrl, settingsProof = null) {
  if (settingsProof?.agent || request.task?.agentMode || isAgentComposer(observation)) throw tag("当前仍是 Flow 智能体模式；先切换到底部普通视频模式并重新核实参数，不得发送聊天请求", { code: "wrong_composer_mode" });
  if (!freshProjectUrl || observation.url !== freshProjectUrl || !/\/project\//.test(observation.url)) throw tag("AI 未创建独立的新 Flow 项目，拒绝生成", { code: "fresh_project_missing" });
  const element = observation.elements.find((item) => item.id === action.element);
  const directGenerate = /^(?:arrow_forward\s*)?(?:Create|Generate|开始生成|生成|创建)(?:\s+\d+)?$/i.test(element?.label.trim() || "");
  const agentSend = settingsProof && /^(?:arrow_forward|send|发送|发送消息|发送提示|提交提示|提交提示词|开始生成|生成)$/i.test(element?.label.trim() || "");
  if (!element || element.disabled || !(directGenerate || agentSend)) throw tag("AI 选择的元素不是已观察到的生成/Flow 智能体发送按钮", { code: "invalid_generate_control" });
  const inputs = observation.elements.filter((item) => item.tag === "textarea" || item.role === "textbox" || item.value !== undefined);
  if (!inputs.some((item) => String(item.value || "").trim() === (request.browserPrompt || request.prompt).trim())) throw tag("Flow 输入框实际提示词与任务不一致，拒绝生成", { code: "prompt_mismatch" });
  if (observation.elements.some((item) => item.tag === "button" && !/menuitem|option|radio/.test(item.role || "") && /Veo|Omni\s*(?!1\.1)\d/i.test(item.inner_text || item.label) && /Video|视频|arrow_drop_down/i.test(item.inner_text || item.label))) throw tag("当前可见模型已不是 Omni 1.1 Flash，旧证明不能放行", { code: "model_not_verified" });
  if (settingsProof?.model === "Omni 1.1 Flash" && settingsProof.observed_url === freshProjectUrl) {
    const current = observation.elements.find((item) => item.tag === "button" && /Video|视频/i.test(item.inner_text || item.label) && /\d+\s*(?:秒|s)/i.test(item.inner_text || item.label));
    if (current) {
      const text = current.inner_text || current.label;
      const duration = Number(/(?:^|\D)(\d+)\s*(?:秒|s)(?:\D|$)/i.exec(text)?.[1]);
      const ratio = /16:9|crop_16_9/.test(text) ? "16:9" : /9:16|crop_9_16/.test(text) ? "9:16" : null;
      const outputs = Number(/(?:x|×)\s*(\d+)(?:\D|$)/i.exec(text)?.[1]);
      if (duration && duration !== request.duration || ratio && ratio !== request.aspectRatio || outputs && outputs !== request.outputs) throw new Error("当前视频时长/比例/数量已变化，旧设置证明不能放行");
    }
    if (settingsProof.duration !== request.duration) throw new Error("Flow 页面未确认请求的视频时长");
    if (settingsProof.aspectRatio !== request.aspectRatio) throw new Error("Flow 页面未确认请求的画面比例");
    if (settingsProof.outputs !== request.outputs) throw new Error("Flow 页面未确认请求的输出数量");
    return;
  }
  const setting = observation.elements.find((item) => item.tag === "button" && /Omni\s*(?:1\.1\s*)?Flash/i.test(item.label) && /Video|视频|Omni/i.test(item.label));
  if (!setting || !/Omni\s*1\.1\s*Flash/i.test(setting.label)) throw tag("Flow 当前模型未明确显示 Omni 1.1 Flash，拒绝生成其他模型", { code: "model_not_verified" });
  if (/Veo|Omni\s*(?!1\.1)\d/i.test(setting.label)) throw tag("Flow 模型不是 Omni 1.1 Flash", { code: "model_not_verified" });
  const text = `${setting.label}\n${observation.text}`;
  if (!new RegExp(`(?:^|\\D)${request.duration}\\s*(?:s|秒)(?:\\D|$)`, "i").test(text)) throw tag("Flow 页面未确认请求的视频时长", { code: "duration_not_verified" });
  if (!text.includes(request.aspectRatio)) throw tag("Flow 页面未确认请求的画面比例", { code: "aspect_not_verified" });
  if (!new RegExp(`(?:x|×)\\s*${request.outputs}(?:\\D|$)|${request.outputs}\\s*(?:outputs?|个|条)`, "i").test(text)) throw tag("Flow 页面未确认请求的输出数量", { code: "outputs_not_verified" });
}

export async function generationEvidence(browser, page, before) {
  const media = await browser.findNewMedia(before).catch(() => []);
  if (media.length) return { evidence: "new_video_media" };
  // Flow's assistant reply/spinner lives in the right panel. A real render
  // produces a media card in the canvas. Do not mistake chat for cloud work.
  const marker = await page.evaluate(() => {
    const width = innerWidth;
    for (const node of document.querySelectorAll('[role="progressbar"],span,p,button,div')) {
      const box = node.getBoundingClientRect();
      if (!box.width || !box.height || box.x + box.width / 2 > width * 0.75) continue;
      const text = (node.innerText || node.getAttribute("aria-label") || "").trim();
      if (node.getAttribute("role") === "progressbar" || /^\d{1,3}\s*%$/.test(text) || /^(Cancel generation|Stop generation|取消生成|停止生成)$/i.test(text)) return text || "media_progressbar";
    }
    return null;
  });
  return marker ? { evidence: "media_canvas_progress", marker } : null;
}

export function agentSettingsProof(observation, request = {}) {
  const model = observation.elements.find((item) => item.tag === "button" && /^Omni\s*1\.1\s*Flash(?:\s+arrow_drop_down)?$/i.test((item.inner_text || item.label).trim()) && !/menuitem|option/.test(item.role || ""));
  if (!model) return null;
  const summary = observation.elements.find((item) => item.tag === "button" && /视频|Video/i.test(item.inner_text || item.label) && /\d+\s*(?:秒|s)/i.test(item.inner_text || item.label));
  const agent = /智能体设置|Agent settings/i.test(observation.text) && /视频生成默认设置|Video generation defaults/i.test(observation.text);
  const selected = observation.elements.filter((item) => item.selected === "true" && (!agent || item.setting_section === "video")).map((item) => item.inner_text || item.label).join(" ");
  const settings = `${summary?.inner_text || summary?.label || ""} ${selected}`;
  const duration = Number(/(?:^|\D)(\d+)\s*(?:秒|s)(?:\D|$)/i.exec(settings)?.[1]);
  const ratio = /(?:16:9|crop_16_9)/.test(settings) ? "16:9" : /(?:9:16|crop_9_16)/.test(settings) ? "9:16" : null;
  const outputs = Number(/(?:x|×)\s*(\d+)(?:\D|$)/i.exec(settings)?.[1]);
  // Agent defaults do not always expose duration. Its exact canonical video
  // instruction is additionally checked when the confirmation card appears.
  return { model: "Omni 1.1 Flash", duration: duration || (agent ? request.duration : null), aspectRatio: ratio, outputs: outputs || null, observed_url: observation.url, verified_at: Date.now(), agent };
}

export function isGenerationControl(label) {
  return /^(?:(?:check\s*)?(?:批准|一律批准|Approve|Approve all)|arrow_forward|send|发送|发送消息|发送提示|提交提示|提交提示词|Confirm|确认|确认生成|生成视频|创建视频|Regenerate|重新生成|再次生成|重试生成|(?:arrow_forward\s*)?(?:Create|Generate|开始生成|生成|创建)(?:\s+\d+)?)$/i.test(label.trim());
}

export function isAgentComposer(observation) {
  // Closing the chat sidebar does NOT switch the bottom composer out of Agent.
  // A direct video header positively identifies the ordinary composer even
  // though the app also exposes an unrelated Agent toggle elsewhere.
  const direct = observation.elements.some((item) => item.tag === "button" && /视频|Video/i.test(item.inner_text || item.label) && /\d+\s*(?:秒|s)/i.test(item.inner_text || item.label));
  if (direct) return false;
  return /智能体设置|Agent settings|未命名的会话|您希望创作什么|您想创作什么/i.test(observation.text) || observation.elements.some((item) => /article_spark/.test(item.inner_text || item.label));
}

export function validateConfirmation(request, observation, action, projectUrl, proof) {
  const element = observation.elements.find((item) => item.id === action.element);
  const text = element?.card_text || "";
  if (!proof || proof.observed_url !== projectUrl || /Veo|图片|image/i.test(text)) throw new Error("视频确认卡片模型或项目不符合请求");
  if (!/Omni\s*1\.1\s*Flash/i.test(text) || !new RegExp(`\\b${request.duration}\\s*(?:s|seconds?|秒)`, "i").test(text) || !text.includes(request.aspectRatio) || !new RegExp(`(?:x|×)\\s*${request.outputs}\\b|${request.outputs}\\s*(?:videos?|outputs?|个|条)`, "i").test(text)) throw new Error("实际视频确认卡片未明确核实 Omni 1.1 Flash、时长、比例和输出数，不能点击收费确认");
}

export function canRecoverSubmission(job) {
  const proof = job.settingsProof;
  return Boolean(job.submissionUnknown && job.promptSent && job.projectUrl && job.account && proof && !proof.agent && proof.model === "Omni 1.1 Flash" && proof.observed_url === job.projectUrl && proof.duration === job.duration && proof.aspectRatio === job.aspectRatio && proof.outputs === job.outputs);
}

export class AiFlowWorker {
  constructor({ browser, createModel, timeoutMs = 1200000, emit = () => {}, observePage = observe, sleep = pause }) {
    this.browser = browser;
    this.createModel = createModel;
    this.timeoutMs = timeoutMs;
    this.emit = emit;
    this.observePage = observePage;
    this.sleep = sleep;
    this.queue = Promise.resolve();
    this.queued = 0;
    this.active = false;
    this.stage = "idle";
  }
  load() { return this.queued + Number(this.active || this.loginPromise || this.browser.stage === "interactive_login"); }
  stats() { return { active: Boolean(this.active || this.loginPromise || this.browser.stage === "interactive_login"), queued: this.queued, stage: this.loginPromise || this.browser.stage === "interactive_login" ? "interactive_login" : this.stage, persistent_profile: true }; }
  async assertPage(page) {
    const url = new URL(page.url());
    if (!["flow.google.com", "labs.google"].includes(url.hostname)) throw tag("Flow 标签页已离开 Google Flow，不能继续操作", { code: "login_required" });
    if (url.hostname !== "flow.google.com" || /^\/(?:project\/[^/]+(?:\/|$))?$/.test(url.pathname)) return this.browser.assertUsableSession();
    // New Flow asset/detail routes are authenticated application pages too.
    // A path change alone is not evidence of logout or account ineligibility.
    const text = await page.locator("body").innerText({ timeout: 10000 });
    if (/\/(?:signin|sign-in|login|auth)(?:\/|$)/i.test(url.pathname) || /Try Google Flow\s+with a Google Account|Couldn't sign you in|not available in your country|登录以继续/i.test(text)) throw tag("Flow 当前页面显示登录入口或账号不可用；请核对实际页面，不代表 Cookie 文件过期", { code: "login_required" });
    if (!/下载|Download|PRO|所有媒体|视频|Video/i.test(text)) throw tag("Flow 未显示可识别的媒体详情页面", { code: "unexpected_flow_page" });
  }
  async inspect() {
    if (this.inspectPromise) return this.inspectPromise;
    this.inspectPromise = this.inspectUnlocked().finally(() => { this.inspectPromise = null; });
    return this.inspectPromise;
  }
  async inspectUnlocked() {
    if (this.loginPromise || this.browser.stage === "interactive_login") return { ready: false, busy: true, error_code: "interactive_login_pending", error: "该账号正在专用浏览器人工登录；不干预窗口、不提交视频" };
    if (this.load()) return { ready: this.submitted === true, busy: true };
    if (this.browser.canAcquireCapacity?.() === false) return { ready: false, busy: true, error_code: "browser_capacity_busy", error: "单浏览器正在处理其他账号；不另开 Chrome" };
    let model;
    let inspectedPage;
    try {
      await this.browser.acquireCapacity?.();
      model = this.createModel();
      const ai = await model.request({ check: true });
      if (this.browser.stage === "interactive_login") return { ready: false, busy: true, error_code: "interactive_login_pending", error: "该账号正在专用浏览器人工登录；不干预窗口、不提交视频" };
      const page = await this.browser.ensurePage({ validate: false });
      inspectedPage = page;
      await this.assertPage(page);
      const observation = await observe(page);
      const canEnter = /\/project\//.test(page.url()) || observation.elements.some((item) => /New project|Create project|新建项目|创建项目/i.test(item.label));
      if (!canEnter) throw new Error("Flow 页面已打开但没有可用项目入口");
      const identity = observation.elements.find((item) => /Google 账号：|Google Account:/i.test(item.label));
      const email = identity?.label.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0] || null;
      return { ready: true, url: page.url(), ai_model: ai.model, cookie_state: "authenticated", authentication_mode: "persistent_browser_profile", account_email: email };
    } catch (error) {
      let inspectedUrl = null;
      try { const parsed = new URL(inspectedPage?.url()); inspectedUrl = parsed.origin + parsed.pathname; } catch {}
      return { ready: false, cookie_state: /auth|sign.in|登录|session/i.test(error.message) ? "login_required" : "unknown", authentication_mode: "persistent_browser_profile", url: inspectedUrl, error_code: error.code || "readiness_check_failed", error: error.message };
    } finally { try { model?.close(); } finally { await this.browser.close?.(); } }
  }
  async close() { await this.browser.close(); }
  assertLoginIdle(allowInteractive = false, allowOpening = false) { if ((!allowOpening && this.loginPromise) || this.active || this.queued || (!allowInteractive && this.browser.stage === "interactive_login")) throw new Error("Flow AI 子任务正在使用该账号，不能关闭生成标签页进行登录"); }
  beginInteractiveLogin(options) {
    this.assertLoginIdle();
    if (this.browser.canAcquireCapacity?.() === false) throw new Error("当前单浏览器正在处理其他账号；结束后再打开登录窗口");
    this.loginPromise = (async () => {
      if (this.inspectPromise) await this.inspectPromise;
      this.assertLoginIdle(false, true);
      if (this.browser.canAcquireCapacity?.() === false) throw new Error("当前单浏览器正在处理其他账号；结束后再打开登录窗口");
      return this.browser.beginInteractiveLogin(options);
    })().finally(() => { this.loginPromise = null; });
    return this.loginPromise;
  }
  loginOperation(action) {
    this.assertLoginIdle(true);
    this.loginPromise = Promise.resolve().then(() => this.browser[action]()).finally(() => { this.loginPromise = null; });
    return this.loginPromise;
  }
  finishInteractiveLogin() { return this.loginOperation("finishInteractiveLogin"); }
  cancelInteractiveLogin() { return this.loginOperation("cancelInteractiveLogin"); }
  run(request, lifecycle = {}) {
    // Shared across pool rotations; enqueue time, not browser start time,
    // defines the last moment at which a new paid submission may begin.
    const ownGuard = !request.resume && !request.submissionGuard;
    let guard;
    try { guard = request.resume ? null : (request.submissionGuard ||= new SubmissionGuard(request.task)); }
    catch (error) { return Promise.reject(error); }
    this.queued++;
    const operation = async () => {
      let acquired = false;
      let started = false;
      let model;
      const task = request.task;
      let page;
      const update = (phase, details = {}) => {
        this.stage = phase;
        lifecycle.onStage?.({ stage: phase, ...details });
        this.emit(task, phase, details);
      };
      try {
        guard?.assert();
        if (this.inspectPromise) await this.inspectPromise;
        guard?.assert();
        if (this.loginPromise || this.browser.stage === "interactive_login") throw tag("账号正在人工登录，不能提交视频", { code: "interactive_login_pending", submissionStarted: false });
        await this.browser.acquireCapacity?.();
        acquired = true;
        guard?.assert();
        this.queued--; started = true; this.active = true; this.submitted = Boolean(request.resume && !request.recover);
        if (!request.resume) {
          // Account rotation shares the job, not the previous account's UI proof.
          for (const key of ["newProjectClicked", "existingProjects", "settingsProof", "projectUrl", "before", "agentMode", "pageId"]) delete task[key];
        }
        lifecycle.onStart?.({ stage: "ai_browser_starting" });
        guard?.assert();
        model = this.createModel();
        guard?.assert();
        page = await this.browser.ensurePage({ validate: false });
        guard?.assert();
        await page.setViewportSize({ width: 1440, height: 1000 });
        await this.assertPage(page);
        let freshProjectUrl = request.resume ? task.projectUrl : "";
        let before = request.resume ? new Set(task.before || []) : null;
        const history = [];
        let lastError = "";
        let confirmationModelFailures = 0;
        let downloadPaths = [...(task.outputPaths || [])];
        if (request.resume) {
          // Resume the existing cloud task. Never submit another generation.
          await page.goto(task.projectUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
        } else if (/\/project\//.test(page.url())) {
          const parsed = new URL(page.url());
          await page.goto(parsed.hostname === "flow.google.com" ? parsed.origin + "/" : parsed.origin + "/fx/tools/flow", { waitUntil: "domcontentloaded", timeout: 60000 });
        }
        task.pageId = request.resume ? (task.pageId || crypto.randomUUID()) : crypto.randomUUID();
        task.submitTaskId ||= `submit_${task.id}`;
        const submitStarted = Date.now();
        while (!this.submitted) {
          guard?.assert();
          const stepStarted = Date.now();
          if (!task.promptSent && Date.now() - submitStarted > 240000) throw tag("Flow 浏览器提交子任务在 4 分钟内未完成配置", { code: "submission_setup_timeout" });
          if (request.recover && Date.now() - submitStarted > 60000) throw tag("重新观察原项目 60 秒后，仍未找到真实视频或生成进度；不重复提交、不称为云端排队", { code: "recovery_no_evidence" });
          if (!request.recover && task.promptSent && Date.now() - (task.sentAt || submitStarted) > this.timeoutMs) throw tag("Flow 请求已点击，但 20 分钟内没有确认真实生成进度；保留原项目，不重复生成", { code: "submission_confirmation_timeout" });
          const observation = await this.observePage(page);
          guard?.assert();
          task.lastObservedAt = Math.floor(Date.now() / 1000);
          this.emit(task, "observation", { text: observation.text.slice(-2200), screenshot: observation.screenshot, elements: observation.elements, url: observation.url, pageId: task.pageId });
          await this.assertPage(page);
          task.agentMode = isAgentComposer(observation);
          delete request.browserPrompt;
          const proof = agentSettingsProof(observation, request);
          if (proof) task.settingsProof = proof;
          if (task.promptSent) {
            const providerError = await this.browser.detectError(observation.text);
            if (providerError) throw tag(`Flow 页面拒绝生成：${providerError}`, { code: "flow_provider_error" });
            const evidence = await generationEvidence(this.browser, page, before);
            if (evidence) {
              this.submitted = true; this.browser.submissionStarted = true;
              task.submissionUnknown = false; task.reviewTaskId = `review_${task.id}`;
              update("ai_reviewing", { submissionStarted: true, submissionEvidence: evidence.evidence, projectUrl: page.url(), pageId: task.pageId });
              break;
            }
            if (request.recover) {
              // Recovery is intentionally observation-only: never repeat a paid
              // click whose confirmation was interrupted.
              await this.sleep(REVIEW_INTERVAL_MS);
              continue;
            }
          }
          if (!freshProjectUrl && task.newProjectClicked && /\/project\//.test(observation.url) && !task.existingProjects?.includes(observation.url)) {
            freshProjectUrl = observation.url;
            task.projectUrl = freshProjectUrl;
            before = await this.browser.snapshotMedia();
            task.before = [...before];
          }
          update("ai_submitting", { pageId: task.pageId, projectUrl: freshProjectUrl });
          const phase = task.promptSent ? "review" : "submit";
          let reply;
          try {
            reply = await model.request({ phase, request: { prompt: request.browserPrompt || request.prompt, duration: request.duration, aspectRatio: request.aspectRatio, outputs: request.outputs, downloaded: downloadPaths.length, submission_confirmed: false, firstFrame: Boolean(request.firstFramePath), flow_request_sent: Boolean(task.promptSent), generation_confirm_clicked: Boolean(task.confirmClicked), settings_verified: Boolean(task.settingsProof) }, history: history.slice(-12), last_error: lastError, ...observation });
            confirmationModelFailures = 0;
          } catch (error) {
            if (phase !== "review") throw error;
            if (/^model_bridge_/.test(error.code || "")) throw error;
            lastError = error.message;
            this.emit(task, "review_model_error", { error: lastError });
            if (++confirmationModelFailures >= 3) throw tag(`Flow 确认期间 AI 连续判断失败：${lastError}`, { code: "review_model_unavailable" });
            await this.sleep(Math.max(0, REVIEW_INTERVAL_MS - (Date.now() - stepStarted)));
            continue;
          }
          guard?.assert();
          const action = reply.decision;
          this.record(task, phase, action, reply, observation);
          try {
            if (action.action === "error") throw tag(action.reason, { terminal: true, code: "flow_page_error" });
            if (action.action === "submit") {
              if (task.promptSent) throw new Error("Flow 请求已发送；只能观察/确认该请求，不得再次提交");
              const current = await this.revalidate(page, observation, action);
              guard?.assert();
              validateSubmit(request, current.observation, current.action, freshProjectUrl, task.settingsProof);
              const target = this.locator(page, current.observation, current.action);
              // Mark ambiguous clicks before input is dispatched. Rotation must
              // never produce duplicate cloud jobs after a lost confirmation.
              guard?.assert();
              task.submissionUnknown = true;
              update("ai_confirming_submission", { submissionUnknown: true });
              await target.click({ timeout: 10000 });
              task.promptSent = true;
              task.sentAt = Date.now();
              update("ai_waiting_for_flow_confirmation", { projectUrl: page.url() });
            } else if (action.action === "confirm") {
              if (!task.promptSent || task.confirmClicked) throw new Error("只允许确认已发送请求一次");
              const element = observation.elements.find((item) => item.id === action.element);
              if (!element || !/生成|确认|批准|Generate|Create|Confirm|Approve/i.test(element.label) || /一律批准|Approve all|图片|image|Veo/i.test(element.label)) throw new Error("只能点击实际视频生成确认按钮");
              const current = await this.revalidate(page, observation, action);
              validateConfirmation(request, current.observation, current.action, freshProjectUrl, task.settingsProof);
              task.confirmClicked = true;
              await this.locator(page, observation, action).click({ timeout: 10000 });
              update("ai_confirming_generation", { projectUrl: page.url() });
            } else {
              if (task.promptSent && ["fill", "upload"].includes(action.action)) throw new Error("Flow 请求已发送，禁止修改或重复发送提示词");
              const selected = observation.elements.find((item) => item.id === action.element);
              if (phase === "submit" && action.action === "click" && /^(?:add\s*)?(?:New project|Create project|新建项目|创建项目)$/i.test(selected?.label.trim() || "")) {
                task.existingProjects = await page.evaluate(() => [...document.querySelectorAll('a[href*="/project/"]')].map((node) => node.href));
                task.newProjectClicked = true;
              }
              const downloaded = await this.act(page, observation, action, request, phase);
              if (downloaded) {
                downloadPaths.push(downloaded);
                this.submitted = true; this.browser.submissionStarted = true;
                task.submissionUnknown = false; task.reviewTaskId = `review_${task.id}`;
                update("ai_reviewing", { submissionStarted: true, submissionEvidence: "downloaded_video", projectUrl: page.url(), pageId: task.pageId });
              }
            }
            lastError = "";
          } catch (error) {
            if (error.preventAccountRetry || error.terminal || task.submissionUnknown && !task.promptSent) throw error;
            lastError = error.message;
            this.emit(task, "action_rejected", { action: action.action, error: lastError });
          }
          history.push({ action: action.action, element: action.element, reason: action.reason, error: lastError });
          if (phase === "review" && action.action === "wait") await this.sleep(Math.max(0, REVIEW_INTERVAL_MS - (Date.now() - stepStarted)));
        }
        // The same AI owns the same page throughout generation and download.
        // Only wait decisions use the five-second cadence. All other replies
        // execute immediately and the next inference gets a post-action snapshot.
        task.reviewTaskId = `review_${task.id}`;
        if (downloadPaths.length >= request.outputs) {
          update("completed", { outputCount: downloadPaths.length });
          return { outputPaths: downloadPaths, outputPath: downloadPaths[0], projectUrl: freshProjectUrl };
        }
        const reviewStarted = Date.now();
        let reviewError = "";
        let reviewFailures = 0;
        while (Date.now() - reviewStarted < this.timeoutMs) {
          const stepStarted = Date.now();
          const observation = await this.observePage(page);
          task.lastObservedAt = Math.floor(Date.now() / 1000);
          task.observationCount = (task.observationCount || 0) + 1;
          const providerError = await this.browser.detectError(observation.text);
          await this.assertPage(page);
          if (providerError) throw tag(`Flow 页面生成失败：${providerError}`, { code: "flow_provider_error" });
          const percentages = [...observation.text.matchAll(/\b(\d{1,3})\s*%/g)].map((match) => Number(match[1])).filter((value) => value >= 0 && value <= 100);
          lifecycle.onHeartbeat?.({ stage: "ai_reviewing", elapsedMs: Date.now() - reviewStarted, progress: percentages[0], observedAt: task.lastObservedAt, observationCount: task.observationCount });
          this.emit(task, "observation", { text: observation.text.slice(-1200), screenshot: observation.screenshot, elements: observation.elements, url: observation.url, pageId: task.pageId });
          let reply;
          try {
            reply = await model.request({ phase: "review", request: { prompt: request.prompt, outputs: request.outputs, downloaded: downloadPaths.length, submission_confirmed: true }, history: history.slice(-8), last_error: reviewError, ...observation });
            reviewFailures = 0;
          } catch (error) {
            if (/^model_bridge_/.test(error.code || "")) throw error;
            reviewError = error.message;
            this.emit(task, "review_model_error", { error: reviewError });
            if (++reviewFailures >= 3) throw tag(`Flow 进度 AI 连续审查失败：${reviewError}`, { code: "review_model_unavailable" });
            await this.sleep(Math.max(0, REVIEW_INTERVAL_MS - (Date.now() - stepStarted)));
            continue;
          }
          const action = reply.decision;
          this.record(task, "review", action, reply, observation);
          if (action.action === "error") throw tag(action.reason, { code: "flow_page_error" });
          try {
            const downloaded = await this.act(page, observation, action, request, "review");
            if (downloaded) downloadPaths.push(downloaded);
            reviewError = "";
          } catch (error) { reviewError = error.message; this.emit(task, "action_rejected", { action: action.action, error: reviewError }); }
          history.push({ action: action.action, reason: action.reason, error: reviewError });
          if (downloadPaths.length >= request.outputs) {
            update("completed", { outputCount: downloadPaths.length });
            return { outputPaths: downloadPaths, outputPath: downloadPaths[0], projectUrl: freshProjectUrl };
          }
          if (action.action === "wait") await this.sleep(Math.max(0, REVIEW_INTERVAL_MS - (Date.now() - stepStarted)));
        }
        throw tag("Flow 页面审查已超过 20 分钟，未观察到可下载的完成视频；不能据此判断云端拥堵", { code: "flow_timeout" });
      } catch (error) {
        throw Object.assign(error, { submissionStarted: (started && this.submitted) || Boolean(task?.submissionUnknown), submissionUnknown: Boolean(task?.submissionUnknown) });
      } finally {
        if (ownGuard) guard?.dispose();
        if (!started) this.queued--;
        try { model?.close(); }
        finally {
          try { if (acquired) await this.browser.close?.(); }
          finally { if (started) { this.active = false; this.stage = "idle"; } }
        }
      }
    };
    const accepted = this.queue.then(operation, operation);
    this.queue = accepted.catch(() => {});
    return accepted;
  }
  locator(page, observation, action) {
    const element = observation.elements.find((item) => item.id === action.element);
    if (!element) throw new Error("AI 使用了本次观察中不存在的元素编号");
    return page.locator(`[data-flow-agent="${element.token}"]`);
  }
  async revalidate(page, observation, action) {
    const target = observation.elements.find((item) => item.id === action.element);
    const current = await this.observePage(page, { screenshot: false });
    const element = current.elements.find((item) => item.token === target?.token);
    if (current.url !== observation.url || !element || element.label !== target.label || (element.selected ?? null) !== (target.selected ?? null) || (element.expanded ?? null) !== (target.expanded ?? null)) throw new Error("提交目标或页面已经变化，请根据新观察重新决定");
    return { observation: current, action: { ...action, element: element.id } };
  }
  record(task, phase, action, reply, observation) {
    this.emit(task, "ai_action", { phase, action: action.action, element: action.element, reason: String(action.reason || "").slice(0, 500), aiModel: reply.model, aiLatencyMs: reply.latency_ms, usage: reply.usage, url: observation.url });
  }
  async act(page, observation, action, request, phase) {
    const name = action.action;
    const downloadOnly = phase === "review" || phase === "download";
    if (name === "wait") return;
    if (name === "press") {
      if (!["Escape", "Tab", "ArrowDown", "ArrowUp"].includes(action.key)) throw new Error("只能使用 Escape/Tab/方向键；生成必须走 submit");
      await page.keyboard.press(action.key); return;
    }
    if (name === "scroll") { await page.mouse.wheel(0, Math.max(-900, Math.min(900, action.scroll_y || 600))); return; }
    if (downloadOnly && !["click", "download"].includes(name)) throw new Error("下载任务不得修改提示词或重复生成");
    if (page.url && page.url() !== observation.url && observation.url) throw new Error("页面已跳转，请根据新观察决定操作");
    const element = observation.elements.find((item) => item.id === action.element);
    const target = this.locator(page, observation, action);
    if (!await target.isVisible()) throw new Error("页面已经变化，目标元素已失效，请根据新观察重试");
    const currentLabel = await target.evaluate((node) => (node.getAttribute("aria-label") || node.getAttribute("alt") || node.innerText || node.getAttribute("placeholder") || "").trim().slice(0, 500));
    if (currentLabel !== element.label) throw new Error("页面目标文字已改变，请根据新观察重新决定操作");
    const currentSelection = await target.evaluate((node) => node.getAttribute("aria-selected") || node.getAttribute("aria-checked") || node.getAttribute("aria-pressed") || (node.getAttribute("data-state") === "active" || node.getAttribute("data-state") === "checked" ? "true" : null));
    if ((element.selected ?? null) !== currentSelection) throw new Error("控件开关状态已变化，请重新观察，不能根据旧截图反向切回智能体模式");
    const currentExpanded = await target.evaluate((node) => node.getAttribute("aria-expanded"));
    if ((element.expanded ?? null) !== currentExpanded) throw new Error("菜单展开状态已变化，请根据新观察决定操作");
    if (name === "click") {
      const visibleText = element.inner_text || element.label;
      if (isGenerationControl(element.label) || isGenerationControl(visibleText) || /New project|新建项目|创建项目/i.test(visibleText) && downloadOnly) throw new Error("生成按钮必须走 submit/confirm；下载中禁止创建项目");
      if (/Veo|Omni\s*(?!1\.1)\d/i.test(visibleText) && (/menuitem|option|radio/.test(element.role || "") || element.tag === "button" && !/arrow_drop_down|视频|Video|设置/i.test(visibleText))) throw new Error("禁止选择 Omni 1.1 Flash 之外的视频模型");
      if (phase === "submit" && (/menuitem|option|radio/.test(element.role || "") || /^(?:\d+\s*(?:秒|s)|\d+:\d+|(?:x|×)\d+|Omni.*|Veo.*)$/i.test(visibleText.trim()))) request.task.settingsProof = null;
      if (element.tag === "a") {
        const href = await target.getAttribute("href");
        if (href && !/^(?:https:\/\/(?:labs|flow)\.google(?:\.com)?\/|\/|#)/i.test(href)) throw new Error("Flow 子任务不能跳转到其他网站");
      }
      await target.click({ timeout: 8000 }); return;
    }
    if (name === "fill") {
      const editable = await target.evaluate((node) => node.isContentEditable || /^(INPUT|TEXTAREA)$/.test(node.tagName));
      if (!editable) throw new Error("只能向实际可编辑提示输入框填入任务提示词");
      await target.fill(request.browserPrompt || request.prompt); return;
    }
    if (name === "upload") {
      if (!request.firstFramePath || phase !== "submit") throw new Error("任务没有给定本地首帧");
      await this.browser.attachFirstFrame(request.firstFramePath); return;
    }
    if (name === "download") {
      if (!downloadOnly || !/download|下载|original|原始|720p|1080p/i.test(element.label)) throw new Error("下载操作必须选择实际视频下载入口");
      if (element.has_popup && element.has_popup !== "false") throw new Error("这是下载菜单开关，不是文件下载选项；先用 click 打开菜单，再对原始视频选项使用 download");
      await fs.mkdir(request.outputDir, { recursive: true });
      const destination = path.join(request.outputDir, `${request.task.id}-${crypto.randomBytes(4).toString("hex")}.mp4`);
      const downloadPromise = page.waitForEvent("download", { timeout: 18000 });
      // Install the listener before input. The same page/context remains open
      // until saveAs and validation both finish.
      const downloaded = Promise.all([downloadPromise, target.click({ timeout: 8000 })]);
      const [download] = await downloaded;
      const failure = await download.failure();
      if (failure) throw new Error(`Flow 浏览器下载失败：${failure}`);
      await download.saveAs(destination);
      const digest = crypto.createHash("sha256").update(await fs.readFile(destination)).digest("hex");
      request.task.downloadDigests ||= [];
      if (request.task.downloadDigests.includes(digest)) {
        await fs.unlink(destination);
        throw new Error("该视频已经下载，不能重复计为另一个输出");
      }
      const handle = await fs.open(destination, "r");
      try {
        const bytes = Buffer.alloc(12); const { bytesRead } = await handle.read(bytes, 0, 12, 0);
        if (bytesRead < 12 || !["ftyp", "moov", "mdat", "wide"].includes(bytes.toString("ascii", 4, 8))) throw new Error("下载内容不是有效的 MP4 视频");
      } finally { await handle.close(); }
      request.task.downloadDigests.push(digest);
      request.task.outputPaths ||= [];
      request.task.outputPaths.push(destination);
      this.emit(request.task, "download_saved", { filename: path.basename(destination), bytes: (await fs.stat(destination)).size });
      return destination;
    }
    throw new Error(`不支持的浏览器操作：${name}`);
  }
}
