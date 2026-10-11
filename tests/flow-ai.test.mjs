import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { validateSubmit, validateConfirmation, agentSettingsProof, generationEvidence, AiFlowWorker, REVIEW_INTERVAL_MS, isAgentComposer, observe, canRecoverSubmission } from "../scripts/flow-ai/browser-agent.mjs";

const request = { prompt: "An orange cat waves.", duration: 8, aspectRatio: "16:9", outputs: 1 };
const url = "https://flow.google.com/project/test-fresh";
test("uncertain recovery requires a verified direct click and cannot recover Flow chat approval", () => {
  const job = { submissionUnknown: true, promptSent: true, projectUrl: url, account: 1, duration: 8, aspectRatio: "16:9", outputs: 1,
    settingsProof: { model: "Omni 1.1 Flash", observed_url: url, duration: 8, aspectRatio: "16:9", outputs: 1, agent: false } };
  assert.equal(canRecoverSubmission(job), true);
  assert.equal(canRecoverSubmission({ ...job, promptSent: false }), false);
  assert.equal(canRecoverSubmission({ ...job, settingsProof: { ...job.settingsProof, agent: true } }), false);
  assert.equal(canRecoverSubmission({ ...job, settingsProof: { ...job.settingsProof, duration: 10 } }), false);
});
test("authenticated tools and media routes do not invoke the obsolete project-only validator", async () => {
  const worker = new AiFlowWorker({ browser: { assertUsableSession: () => { throw new Error("obsolete route rejected"); } }, createModel: () => null });
  for (const suffix of ["/tools", "/edit/video-result"]) {
    await worker.assertPage({ url: () => url + suffix, locator: () => ({ innerText: async () => "PRO 视频 工具 下载" }) });
  }
  await worker.assertPage({ url: () => url + "/edit/authoring-result", locator: () => ({ innerText: async () => "PRO 视频 下载" }) });
  await assert.rejects(worker.assertPage({ url: () => "https://accounts.google.com/signin", locator: () => ({ innerText: async () => "登录" }) }), /离开/);
});
test("observation retains non-button mode controls and actual pressed state", async () => {
  const saved = { document: globalThis.document, getComputedStyle: globalThis.getComputedStyle, location: globalThis.location };
  const attrs = { role: "switch", "aria-pressed": "true", "aria-haspopup": "menu", "aria-controls": "mode-menu", title: "切换创作模式" };
  const node = { tagName: "DIV", innerText: "智能体", parentElement: null, getBoundingClientRect: () => ({ x: 0, y: 0, width: 100, height: 40 }), getAttribute: (name) => attrs[name] || null, setAttribute: (name, value) => { attrs[name] = value; }, closest: () => null };
  globalThis.document = { body: { innerText: "智能体" }, querySelectorAll: (selector) => { for (const role of ["tab", "switch", "combobox"]) assert.ok(selector.includes(`[role="${role}"]`)); return [node]; } };
  globalThis.getComputedStyle = () => ({ visibility: "visible", display: "block" });
  globalThis.location = { href: url };
  try {
    let screenshots = 0;
    const page = { isClosed: () => false, evaluate: async (fn) => fn(), screenshot: async () => { screenshots++; return Buffer.from("image"); } };
    const state = await observe(page);
    assert.equal(state.elements[0].selected, "true");
    assert.equal(state.elements[0].title, "切换创作模式");
    assert.equal(state.elements[0].controls, "mode-menu");
    assert.match(state.screenshot, /^data:image\/jpeg;base64,/);
    const monitor = await observe(page, { screenshot: false });
    assert.equal(monitor.screenshot, undefined);
    assert.equal(monitor.elements[0].selected, "true");
    assert.equal(screenshots, 1, "fresh submission validation should not capture a screenshot");
  } finally { Object.assign(globalThis, saved); }
});
test("closed sidebar does not disguise Flow agent composer or authorize paid submission", () => {
  const state = { url, text: "您希望创作什么？", elements: [{ tag: "button", label: "article_spark" }] };
  assert.equal(isAgentComposer(state), true);
  state.elements.push({ tag: "button", label: "设置触发器", inner_text: "视频 · 720p · 8秒 crop_16_9 x1" });
  assert.equal(isAgentComposer(state), false);
  assert.throws(() => validateSubmit(request, observed(), { element: 2 }, url, { agent: true }), /智能体模式/);
});
function observed(model = "Video · Omni 1.1 Flash · 8s · 16:9 · x1") {
  return { url, text: model, elements: [
    { id: 0, tag: "button", label: model, token: "settings" },
    { id: 1, role: "textbox", label: "", value: request.prompt, token: "prompt" },
    { id: 2, tag: "button", label: "arrow_forward Create", token: "submit" },
  ] };
}
test("submit requires fresh project, exact prompt and explicitly visible Omni 1.1 settings", () => {
  const state = observed();
  assert.doesNotThrow(() => validateSubmit(request, state, { element: 2 }, url));
  assert.throws(() => validateSubmit(request, state, { element: 2 }, ""), /独立的新/);
  state.elements[1].value = "other request";
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url), /提示词/);
});
test("Veo, unspecified Omni versions and wrong duration are refused before generating", () => {
  for (const model of ["Video · Veo 3.1 · 8s · 16:9 · x1", "Video · Omni 1.2 Flash · 8s · 16:9 · x1", "Video · Omni Flash · 8s · 16:9 · x1"]) {
    assert.throws(() => validateSubmit(request, observed(model), { element: 2 }, url), /模型/);
  }
  assert.throws(() => validateSubmit(request, observed("Video · Omni 1.1 Flash · 6s · 16:9 · x1"), { element: 2 }, url), /时长/);
});
test("new-project and invented controls cannot submit", () => {
  const state = observed();
  assert.throws(() => validateSubmit(request, state, { element: 99 }, url), /发送按钮/);
  state.elements[2].label = "New project";
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url), /发送按钮/);
});
test("ordinary composer settings must be observed before sending a video request", () => {
  const state = observed("Omni 1.1 Flash arrow_drop_down");
  state.text = "视频设置";
  state.elements.push({ id: 3, tag: "button", label: "视频 · 720p · 8 秒 crop_16_9 x1" });
  const proof = agentSettingsProof(state, request);
  assert.equal(proof.model, "Omni 1.1 Flash");
  state.elements[2].label = "arrow_forward";
  assert.doesNotThrow(() => validateSubmit(request, state, { element: 2 }, url, proof));
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, { ...proof, duration: 4 }), /时长/);
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, { ...proof, aspectRatio: "9:16" }), /比例/);
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, { ...proof, outputs: 4 }), /数量/);
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, { ...proof, observed_url: url + "old" }), /时长|比例|数量/);
  state.elements[0].label = "Veo 3.1 arrow_drop_down";
  assert.equal(agentSettingsProof(state), null);
});
test("review and download reject send icons and all generation confirmation controls", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  for (const phase of ["review", "download"]) for (const label of ["arrow_forward", "生成视频", "Confirm", "Approve", "确认生成", "send", "批准", "check\n批准", "一律批准"]) {
    const state = { elements: [{ id: 0, label, token: "control" }] };
    const page = mockPage(label);
    await assert.rejects(worker.act(page, state, { action: "click", element: 0 }, request, phase), /submit/);
    assert.equal(page.clicked(), 0);
  }
});
test("interactive login cannot close an active AI-owned page", () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  worker.active = true;
  for (const method of ["beginInteractiveLogin", "finishInteractiveLogin", "cancelInteractiveLogin"]) assert.throws(() => worker[method](), /不能关闭/);
});

test("manual login is reported busy and never launches another page or generation", async () => {
  let verified = 0;
  const browser = { stage: "interactive_login", finishInteractiveLogin: async () => { verified++; }, ensurePage: async () => { throw new Error("must not open page during login"); } };
  const worker = new AiFlowWorker({ browser, createModel: () => { throw new Error("must not call model"); } });
  assert.equal(worker.load(), 1);
  assert.equal(worker.stats().stage, "interactive_login");
  assert.equal((await worker.inspect()).error_code, "interactive_login_pending");
  await assert.rejects(worker.run({ ...request, task: {} }), /人工登录/);
  assert.equal(worker.queued, 0);
  await worker.finishInteractiveLogin();
  assert.equal(verified, 1);
});

test("manual login waits for an in-flight readiness check before opening its window", async () => {
  let releaseCheck;
  let opened = false;
  const checked = new Promise((resolve) => { releaseCheck = resolve; });
  const page = { url: () => "https://flow.google.com/", isClosed: () => false,
    evaluate: async () => ({ text: "PRO", elements: [{ label: "New project" }], url: "https://flow.google.com/" }), screenshot: async () => Buffer.from("image") };
  const browser = { stage: "idle", ensurePage: async () => { assert.equal(opened, false, "automatic page opened during manual login"); return page; }, assertUsableSession: async () => {}, beginInteractiveLogin: async () => { opened = true; browser.stage = "interactive_login"; return { opened: true }; } };
  const worker = new AiFlowWorker({ browser, createModel: () => ({ request: () => checked, close() {} }) });
  const inspect = worker.inspect();
  const login = worker.beginInteractiveLogin();
  assert.equal(opened, false);
  releaseCheck({ model: "test" });
  assert.equal((await inspect).ready, true);
  assert.equal((await login).opened, true);
});

test("readiness rechecks the manual-login lock after awaiting the model", async () => {
  let releaseCheck;
  const checked = new Promise((resolve) => { releaseCheck = resolve; });
  const browser = { stage: "idle", ensurePage: async () => { throw new Error("must not open page after login began"); } };
  const worker = new AiFlowWorker({ browser, createModel: () => ({ request: () => checked, close() {} }) });
  const inspect = worker.inspect();
  browser.stage = "interactive_login";
  releaseCheck({ model: "test" });
  assert.equal((await inspect).error_code, "interactive_login_pending");
});
test("Flow assistant spinner is not proof of a video rendering in the media canvas", async () => {
  const browser = { findNewMedia: async () => [] };
  const savedDocument = globalThis.document, savedWidth = globalThis.innerWidth;
  globalThis.innerWidth = 1440;
  let nodes = [{ getBoundingClientRect: () => ({ x: 1250, width: 20, height: 20 }), getAttribute: () => "progressbar", innerText: "" }];
  globalThis.document = { querySelectorAll: () => nodes };
  try {
    const page = { evaluate: async (callback) => callback() };
    assert.equal(await generationEvidence(browser, page, new Set()), null);
    nodes = [{ getBoundingClientRect: () => ({ x: 260, width: 24, height: 24 }), getAttribute: () => null, innerText: "play_circle" }];
    assert.equal(await generationEvidence(browser, page, new Set()), null, "optimistic gray play card is not a confirmed cloud job");
    nodes = [{ getBoundingClientRect: () => ({ x: 400, width: 30, height: 20 }), getAttribute: () => null, innerText: "25%" }];
    assert.equal((await generationEvidence(browser, page, new Set())).evidence, "media_canvas_progress");
  } finally { globalThis.document = savedDocument; globalThis.innerWidth = savedWidth; }
});
test("visible settings text is retained even when aria-label is only a generic name", () => {
  const state = observed();
  state.elements[0] = { id: 0, tag: "button", label: "选择模型系列", inner_text: "Omni 1.1 Flash\narrow_drop_down" };
  state.elements.push({ id: 3, tag: "button", label: "设置触发器", inner_text: "视频 · 720p · 8 秒\ncrop_16_9\nx1" });
  const proof = agentSettingsProof(state, request);
  assert.equal(proof.duration, 8); assert.equal(proof.aspectRatio, "16:9"); assert.equal(proof.outputs, 1);
  state.elements[2].label = "arrow_forward";
  assert.doesNotThrow(() => validateSubmit(request, state, { element: 2 }, url, proof));
});
test("stale Omni proof cannot override currently visible Veo settings", () => {
  const state = observed(); const proof = { model: "Omni 1.1 Flash", observed_url: url, duration: 8, aspectRatio: "16:9", outputs: 1 };
  state.elements[0] = { id: 0, tag: "button", label: "设置触发器", inner_text: "Video · Veo 3.1 · 8s · 16:9 · x1" };
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, proof), /模型/);
  state.elements[0].inner_text = "Video · Omni 1.1 Flash · 4s · 9:16 · x4";
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, proof), /已变化/);
});
test("stale ordinary-composer proof cannot authorize a currently visible agent composer", () => {
  const proof = { model: "Omni 1.1 Flash", observed_url: url, duration: 8, aspectRatio: "16:9", outputs: 1, agent: false };
  const state = observed("Omni 1.1 Flash arrow_drop_down");
  state.text = "您希望创作什么？";
  state.elements.push({ id: 3, tag: "button", label: "article_spark", token: "agent" });
  assert.equal(isAgentComposer(state), true);
  assert.throws(() => validateSubmit(request, state, { element: 2 }, url, proof), /智能体模式/);
});
test("agent confirmation must verify the actual video proposal rather than old defaults", () => {
  const proof = { model: "Omni 1.1 Flash", observed_url: url };
  const state = { elements: [{ id: 0, label: "确认生成", card_text: "Omni 1.1 Flash 8 seconds 16:9 1 video" }] };
  assert.doesNotThrow(() => validateConfirmation(request, state, { element: 0 }, url, proof));
  state.elements[0].card_text = "Veo 3.1 8 seconds 16:9 1 video";
  assert.throws(() => validateConfirmation(request, state, { element: 0 }, url, proof), /模型/);
  state.elements[0].card_text = "Omni 1.1 Flash 4 seconds 9:16 4 videos";
  assert.throws(() => validateConfirmation(request, state, { element: 0 }, url, proof), /核实/);
});
test("agent video defaults must not inherit the image output count", () => {
  const state = observed("Omni 1.1 Flash arrow_drop_down");
  state.text = "智能体设置 图片生成默认设置 视频生成默认设置";
  state.elements.push({ tag: "button", label: "x2", selected: "true", setting_section: "image" },
    { tag: "button", label: "16:9", selected: "true", setting_section: "video" },
    { tag: "button", label: "x1", selected: "true", setting_section: "video" });
  assert.equal(agentSettingsProof(state, request).outputs, 1);
});
function mockTarget(label, selected = null, click = async () => {}) {
  const node = { innerText: label, getAttribute: (name) => name === "aria-pressed" ? selected : null };
  return { isVisible: async () => true, evaluate: async (callback) => callback(node), click };
}
function mockPage(label, selected = null) {
  let clicked = 0;
  const target = mockTarget(label, selected, async () => { clicked++; });
  return { locator: () => target, clicked: () => clicked };
}

test("a stale expanded menu snapshot cannot toggle the menu closed again", async () => {
  let clicked = 0;
  const node = { innerText: "下载媒体内容", getAttribute: (name) => name === "aria-expanded" ? "true" : null };
  const page = { locator: () => ({ isVisible: async () => true, evaluate: async (callback) => callback(node), click: async () => { clicked++; } }) };
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  await assert.rejects(worker.act(page, { elements: [{ id: 0, token: "menu", label: "下载媒体内容", expanded: "false" }] }, { action: "click", element: 0 }, request, "review"), /展开状态/);
  assert.equal(clicked, 0);
});

test("review inference after a menu action must use a fresh post-action snapshot", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "flow-review-post-action-"));
  let menuOpen = false;
  let decisions = 0;
  const events = [];
  const state = () => ({ url, text: menuOpen ? "视频 Download menu open" : "视频 Download menu closed", elements: [
    { id: 0, tag: "button", label: "下载媒体内容", token: "menu", expanded: String(menuOpen) },
    ...(menuOpen ? [{ id: 1, tag: "button", label: "720p 原始尺寸", token: "original" }] : []),
  ] });
  const page = {
    url: () => url, isClosed: () => false, goto: async () => {}, setViewportSize: async () => {},
    screenshot: async () => Buffer.from("image"),
    evaluate: async () => state(),
    locator: (selector) => {
      if (selector === "body") return { innerText: async () => "视频 Download" };
      const isMenu = selector.includes('"menu"');
      const node = { innerText: isMenu ? "下载媒体内容" : "720p 原始尺寸", getAttribute: (name) => isMenu && name === "aria-expanded" ? String(menuOpen) : null };
      return { isVisible: async () => isMenu || menuOpen, evaluate: async (callback) => callback(node), click: async () => { events.push(isMenu ? "click menu" : "click download"); if (isMenu) menuOpen = !menuOpen; } };
    },
    waitForEvent: async () => ({ failure: async () => null, saveAs: async (filename) => fs.writeFile(filename, Buffer.from([0, 0, 0, 24, ...Buffer.from("ftypisom0000")])) }),
  };
  const model = { close() {}, request: async ({ phase, text }) => {
    decisions++;
    assert.equal(phase, "review");
    events.push(`model ${decisions}`);
    assert.equal(text.includes("menu open"), decisions > 1, "next inference reused the pre-action menu state");
    await Promise.resolve();
    events.push(`reply ${decisions}`);
    return { decision: decisions === 1 ? { action: "click", element: 0 } : { action: "download", element: 1 } };
  } };
  const browser = { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null };
  try {
    const worker = new AiFlowWorker({ browser, createModel: () => model, timeoutMs: 90000,
      observePage: async () => { events.push("observe"); return state(); },
      sleep: async (ms) => assert.fail(`an actionable reply must not wait another ${ms}ms`) });
    const result = await worker.run({ ...request, resume: true, outputDir: directory, task: { id: "video_post_action", projectUrl: url } });
    assert.equal(result.outputPaths.length, 1);
    assert.equal(decisions, 2);
    assert.deepEqual(events, ["observe", "model 1", "reply 1", "click menu", "observe", "model 2", "reply 2", "click download"]);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
for (const inferenceMs of [1250, 7000]) test(`a review wait fills only the remainder of five seconds after a ${inferenceMs}ms model response`, async () => {
  const originalNow = Date.now;
  let now = originalNow();
  const startedAt = now;
  const starts = [], sleeps = [];
  let inFlight = 0;
  const page = { url: () => url, setViewportSize: async () => {}, goto: async () => {} };
  const browser = { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null };
  const model = { close() {}, request: async ({ phase, text, screenshot }) => {
    assert.equal(phase, "review");
    assert.equal(text, "视频 25%");
    assert.ok(screenshot, "every progress decision receives an observed screenshot");
    assert.equal(inFlight++, 0, "progress inference must remain serial");
    starts.push(now - startedAt);
    await Promise.resolve();
    now += inferenceMs;
    inFlight--;
    return { decision: starts.length === 1 ? { action: "wait" } : { action: "error", reason: "stop simulated generation" } };
  } };
  Date.now = () => now;
  try {
    const worker = new AiFlowWorker({ browser, createModel: () => model,
      observePage: async () => ({ url, text: "视频 25%", screenshot: "data:image/jpeg;base64,aW1hZ2U=", elements: [] }),
      sleep: async (ms) => { sleeps.push(ms); now += ms; } });
    await assert.rejects(worker.run({ ...request, resume: true, task: { id: "video_wait", projectUrl: url } }), /stop simulated generation/);
    assert.deepEqual(sleeps, [Math.max(0, 5000 - inferenceMs)]);
    assert.deepEqual(starts, [0, Math.max(5000, inferenceMs)]);
    assert.equal(inFlight, 0);
  } finally { Date.now = originalNow; }
});
test("a closed or timed-out model bridge stops review immediately without retry sleeps", async () => {
  for (const code of ["model_bridge_closed", "model_bridge_timeout"]) {
    let requests = 0, closed = 0;
    const page = { url: () => url, setViewportSize: async () => {}, goto: async () => {} };
    const worker = new AiFlowWorker({ browser: { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null },
      createModel: () => ({ request: async () => { requests++; throw Object.assign(new Error("bridge unavailable"), { code }); }, close() { closed++; } }),
      observePage: async () => ({ url, text: "视频 25%", elements: [] }),
      sleep: async () => assert.fail("a terminated bridge cannot retry or sleep") });
    await assert.rejects(worker.run({ ...request, resume: true, task: { id: "video_bridge", projectUrl: url } }), (error) => error.code === code && error.submissionStarted === true);
    assert.equal(requests, 1);
    assert.equal(closed, 1);
    assert.equal(worker.active, false);
  }
});
test("provider errors and login redirects stop review before another model decision", async () => {
  for (const failure of ["provider", "login"]) {
    let pageUrl = url;
    const page = { url: () => pageUrl, setViewportSize: async () => {}, goto: async () => {} };
    const worker = new AiFlowWorker({ browser: { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => failure === "provider" ? "quota exhausted" : null },
      createModel: () => ({ request: async () => assert.fail("failed pages cannot call the model"), close() {} }),
      observePage: async () => { if (failure === "login") pageUrl = "https://accounts.google.com/signin"; return { url: pageUrl, text: "视频", elements: [] }; },
      sleep: async () => assert.fail("terminal page failures cannot sleep") });
    await assert.rejects(worker.run({ ...request, resume: true, task: { id: "video_failure", projectUrl: url } }), (error) => error.code === (failure === "provider" ? "flow_provider_error" : "login_required"));
  }
});
test("a page redirect during inference invalidates the reply before it can click", async () => {
  let pageUrl = url, clicks = 0, requests = 0;
  const page = { url: () => pageUrl, setViewportSize: async () => {}, goto: async () => {}, locator: () => mockTarget("下载媒体内容", null, async () => { clicks++; }) };
  const worker = new AiFlowWorker({ browser: { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null },
    createModel: () => ({ request: async () => { requests++; pageUrl = "https://accounts.google.com/signin"; return { decision: { action: "click", element: 0 } }; }, close() {} }),
    observePage: async () => ({ url: pageUrl, text: "视频", elements: [{ id: 0, token: "download", label: "下载媒体内容" }] }),
    sleep: async () => assert.fail("a click reply should be validated immediately") });
  await assert.rejects(worker.run({ ...request, resume: true, task: { id: "video_redirect", projectUrl: url } }), (error) => error.code === "login_required");
  assert.equal(requests, 1);
  assert.equal(clicks, 0);
});
test("a submit reply must recheck the fresh prompt and settings before a paid click", async () => {
  for (const changed of ["prompt", "model", "duration", "agent"]) {
    let pageUrl = "https://flow.google.com/", paidClicks = 0, decisions = 0, revalidated = false;
    const rejected = [];
    const state = observed();
    state.elements.push({ id: 3, tag: "button", label: "New project", token: "new" },
      { id: 4, tag: "button", label: "视频 · 720p · 8 秒 crop_16_9 x1", token: "parameters" });
    const page = { url: () => pageUrl, setViewportSize: async () => {}, evaluate: async () => [],
      locator: (selector) => { const item = state.elements.find((element) => selector.includes(`"${element.token}"`)); return mockTarget(item.label, null, async () => {
        if (item.token === "new") pageUrl = url;
        if (item.token === "submit") paidClicks++;
      }); } };
    const browser = { ensurePage: async () => page, assertUsableSession: async () => {}, snapshotMedia: async () => new Set() };
    const worker = new AiFlowWorker({ browser, createModel: () => ({ close() {}, request: async ({ phase }) => {
      assert.equal(phase, "submit");
      if (++decisions === 1) return { decision: { action: "click", element: 3 } };
      if (decisions === 2) {
        if (changed === "prompt") state.elements[1].value = "another prompt";
        if (changed === "model") state.elements[0].label = "Video · Veo 3.1 · 8s · 16:9 · x1";
        if (changed === "duration") { state.elements[0].label = "Video · Omni 1.1 Flash · 4s · 16:9 · x1"; state.elements[4].label = "视频 · 720p · 4 秒 crop_16_9 x1"; state.text = state.elements[0].label; }
        if (changed === "agent") { state.text = "您希望创作什么？"; state.elements[0].label = "Omni 1.1 Flash arrow_drop_down"; state.elements[4] = { id: 4, tag: "button", label: "article_spark", token: "agent" }; }
        return { decision: { action: "submit", element: 2 } };
      }
      return { decision: { action: "error", reason: "stop rejected submission" } };
    } }), observePage: async (_page, options) => {
      if (decisions === 2 && !revalidated) { assert.equal(options?.screenshot, false, "submission must obtain fresh DOM validation after the reply"); revalidated = true; }
      return structuredClone({ ...state, url: pageUrl });
    }, sleep: async () => assert.fail("rejected submit should immediately reobserve"),
    emit: (_task, event, details) => { if (event === "action_rejected") rejected.push(details.error); } });
    await assert.rejects(worker.run({ ...request, task: { id: `video_changed_${changed}` } }), /stop rejected submission/);
    assert.equal(paidClicks, 0, `${changed} changed while the model was reasoning`);
    assert.equal(revalidated, true);
    assert.equal(rejected.length, 1);
    assert.match(rejected[0], /提示词|模型|时长|变化|智能体模式/);
  }
});
test("multiple model-chosen outputs reject a duplicate and count distinct downloads only", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "flow-ai-multiple-"));
  let decisions = 0, downloads = 0;
  const events = [];
  const state = { url, text: "视频 Download", elements: [{ id: 0, label: "Download original video", token: "download" }] };
  const page = { url: () => url, setViewportSize: async () => {}, goto: async () => {}, locator: () => mockTarget("Download original video"),
    waitForEvent: async () => { const suffix = ++downloads < 3 ? "video-a" : "video-b"; return { failure: async () => null,
      saveAs: async (filename) => fs.writeFile(filename, Buffer.concat([Buffer.from([0, 0, 0, 24]), Buffer.from(`ftypisom0000${suffix}`)])) }; } };
  const worker = new AiFlowWorker({ browser: { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null },
    createModel: () => ({ close() {}, request: async ({ phase, request: current, last_error }) => {
      assert.equal(phase, "review");
      assert.equal(current.outputs, 2);
      assert.equal(current.downloaded, decisions === 0 ? 0 : 1);
      if (++decisions === 3) assert.match(last_error, /已经下载/);
      assert.ok(decisions <= 3, "duplicate output retry must be bounded by the supplied decisions");
      return { decision: { action: "download", element: 0 } };
    } }), observePage: async () => state, sleep: async () => assert.fail("download replies must execute without another cadence delay"),
    emit: (_task, event) => events.push(event) });
  try {
    const task = { id: "video_multiple", projectUrl: url };
    const result = await worker.run({ ...request, outputs: 2, resume: true, outputDir: directory, task });
    assert.equal(result.outputPaths.length, 2);
    assert.equal(new Set(result.outputPaths).size, 2);
    assert.equal(task.downloadDigests.length, 2);
    assert.equal((await fs.readdir(directory)).length, 2);
    assert.equal(events.filter((event) => event === "download_saved").length, 2);
    assert.equal(decisions, 3);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});
test("review cannot generate again, alter the prompt or create another project", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  const state = observed(); const page = mockPage("arrow_forward Create");
  await assert.rejects(worker.act(page, state, { action: "submit", element: 2 }, request, "review"), /不得修改/);
  await assert.rejects(worker.act(page, state, { action: "click", element: 2 }, request, "review"), /submit/);
  await assert.rejects(worker.act(page, state, { action: "fill", element: 1 }, request, "review"), /不得修改/);
  assert.equal(page.clicked(), 0);
  assert.equal(REVIEW_INTERVAL_MS, 5000);
});
test("reused DOM nodes with changed labels cannot receive stale AI clicks", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  await assert.rejects(worker.act(mockPage("different"), observed(), { action: "click", element: 0 }, request, "review"), /目标文字已改变/);
});
test("changed pressed state rejects a stale click instead of toggling back", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  const state = { elements: [{ id: 0, label: "Agent", selected: "true", token: "agent-toggle" }] };
  const page = mockPage("Agent", "false");
  await assert.rejects(worker.act(page, state, { action: "click", element: 0 }, request, "submit"), /开关状态已变化/);
  assert.equal(page.clicked(), 0);
});
test("download is observed first, saved to the workspace, and rejects non-video data", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "flow-ai-test-"));
  const events = [];
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null, emit: (_task, event) => events.push(event) });
  const taskRequest = { ...request, outputDir: directory, task: { id: "video_test" } };
  const state = { elements: [{ id: 1, label: "Download original video", token: "download" }] };
  const page = mockPage("Download original video");
  let listenerInstalled = false;
  page.waitForEvent = async () => {
    listenerInstalled = true;
    return { failure: async () => null, saveAs: async (filename) => fs.writeFile(filename, Buffer.from([0, 0, 0, 24, ...Buffer.from("ftypisom0000")])) };
  };
  page.locator = () => mockTarget("Download original video", null, async () => assert.equal(listenerInstalled, true));
  const saved = await worker.act(page, state, { action: "download", element: 1 }, taskRequest, "review");
  assert.equal(path.dirname(saved), directory);
  assert.deepEqual(events, ["download_saved"]);
  await assert.rejects(worker.act(page, state, { action: "download", element: 1 }, taskRequest, "review"), /已经下载/);
  assert.equal((await fs.readdir(directory)).length, 1);
  page.waitForEvent = async () => ({ failure: async () => null, saveAs: async (filename) => fs.writeFile(filename, "<html>not a video</html>") });
  await assert.rejects(worker.act(page, state, { action: "download", element: 1 }, taskRequest, "review"), /不是有效的 MP4/);
  await fs.rm(directory, { recursive: true, force: true });
});
test("download menu trigger cannot be mistaken for the actual file download option", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => null });
  const state = { elements: [{ id: 0, label: "下载媒体内容", token: "menu", has_popup: "menu" }] };
  const page = mockPage("下载媒体内容");
  await assert.rejects(worker.act(page, state, { action: "download", element: 0 }, request, "review"), /菜单开关/);
  assert.equal(page.clicked(), 0);
});
test("busy authenticated review is ready without navigating its page", async () => {
  const worker = new AiFlowWorker({ browser: {}, createModel: () => { throw new Error("must not start another model"); } });
  worker.active = true; worker.submitted = true;
  assert.deepEqual(await worker.inspect(), { ready: true, busy: true });
});
test("an idle worker can inspect the downloaded detail page without the legacy URL validator", async () => {
  let validated;
  const page = { url: () => url + "/edit/video1", locator: () => ({ innerText: async () => "视频 Download Omni 1.1 Flash" }), isClosed: () => false,
    evaluate: async () => ({ text: "视频 Download", elements: [], url: url + "/edit/video1" }), screenshot: async () => Buffer.from("image") };
  const browser = { ensurePage: async (options) => { validated = options.validate; return page; }, assertUsableSession: async () => { throw new Error("legacy rejected detail"); }, context: { cookies: async () => [] } };
  const worker = new AiFlowWorker({ browser, createModel: () => ({ request: async () => ({ model: "test-model" }), close() {} }) });
  const result = await worker.inspect();
  assert.equal(validated, false); assert.equal(result.ready, true);
});

test("paid direct click can confirm after the setup deadline without another model submission", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "flow-ai-lifecycle-"));
  const originalNow = Date.now;
  let now = originalNow();
  let pageUrl = "https://flow.google.com/";
  let clicks = 0;
  const phases = [];
  const task = { id: "video_lifecycle", newProjectClicked: true, existingProjects: ["old-account-project"], settingsProof: { model: "stale" }, projectUrl: "old-account-project", before: ["old-media"] };
  const state = observed();
  state.elements.push({ id: 3, tag: "button", label: "New project", token: "new" },
    { id: 4, tag: "button", label: "Download original video", token: "download" });
  const page = {
    url: () => pageUrl, isClosed: () => false, setViewportSize: async () => {},
    screenshot: async () => Buffer.from("image"),
    evaluate: async (callback) => callback.toString().includes("const visible") ? { ...state, url: pageUrl } : callback.toString().includes("a[href") ? [] : "0%",
    locator: (selector) => {
      const item = state.elements.find((element) => selector.includes(`"${element.token}"`));
      return mockTarget(item.label, null, async () => {
        if (item.token === "new") pageUrl = url;
        if (item.token === "submit") { clicks++; now += 300001; }
      });
    },
    waitForEvent: async () => ({ failure: async () => null, saveAs: async (filename) => fs.writeFile(filename, Buffer.from([0, 0, 0, 24, ...Buffer.from("ftypisom0000")])) }),
  };
  let decisions = 0;
  const model = { close() {}, request: async ({ phase }) => {
    if (phase === "review") return { decision: { action: "download", element: 4 } };
    decisions++;
    return { decision: decisions === 1 ? { action: "click", element: 3 } : { action: "submit", element: 2 } };
  } };
  const browser = { ensurePage: async () => {
    for (const key of ["newProjectClicked", "existingProjects", "settingsProof", "projectUrl", "before"]) assert.equal(task[key], undefined);
    return page;
  }, assertUsableSession: async () => {}, snapshotMedia: async () => new Set(), findNewMedia: async () => [], detectError: async () => null };
  Date.now = () => now;
  try {
    const worker = new AiFlowWorker({ browser, createModel: () => model, observePage: async () => ({ ...state, url: pageUrl }),
      sleep: async (ms) => assert.fail(`confirmed paid submission must not wait ${ms}ms for an actionable model reply`) });
    const result = await worker.run({ ...request, task, outputDir: directory }, { onStage: (stage) => phases.push(stage) });
    assert.equal(clicks, 1);
    assert.equal(decisions, 2);
    assert.ok(phases.some((stage) => stage.submissionEvidence === "media_canvas_progress"));
    assert.equal(result.outputPaths.length, 1);
    assert.ok(task.observationCount >= 1);
  } finally {
    Date.now = originalNow;
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test("uncertain recovery only observes the original project and fails without new paid actions", async () => {
  const originalNow = Date.now;
  let now = originalNow();
  const task = { id: "video_recovery", projectUrl: url, promptSent: true, submissionUnknown: true, account: 1, before: [], sentAt: now - 1200001 };
  const visits = [];
  let requests = 0;
  const state = observed();
  const page = { url: () => url, isClosed: () => false, setViewportSize: async () => {},
    goto: async (destination) => visits.push(destination), screenshot: async () => Buffer.from("image"),
    evaluate: async (callback) => callback.toString().includes("const visible") ? state : null };
  const browser = { ensurePage: async () => page, assertUsableSession: async () => {}, detectError: async () => null,
    findNewMedia: async () => { now += 60001; return []; } };
  const model = { request: async () => { requests++; throw new Error("must not decide another submission"); }, close() {} };
  Date.now = () => now;
  try {
    const worker = new AiFlowWorker({ browser, createModel: () => model, observePage: async () => state, sleep: async () => {} });
    await assert.rejects(worker.run({ ...request, task, resume: true, recover: true }), (error) => error.code === "recovery_no_evidence");
    assert.deepEqual(visits, [url]);
    assert.equal(requests, 0);
    assert.equal(task.promptSent, true);
  } finally { Date.now = originalNow; }
});
