import path from "node:path";

// The Bot uses browser-owned authentication only. Ignore even inherited legacy
// exports before the external session library's configuration is evaluated.
export function configureProfileEnvironment(env = process.env) {
  for (const name of Object.keys(env)) {
    if (/^FLOW_(?:COOKIES_FILE(?:_\d+)?|COOKIES_FILES|STORAGE_STATE(?:_\d+)?|STORAGE_STATES)$/.test(name)) delete env[name];
  }
  const dataDir = path.resolve(env.QQ_AI_DATA_DIR || path.join(env.LOCALAPPDATA || process.cwd(), "QQAIGroupAgent/data"));
  const profileRoot = path.resolve(env.FLOW_PROFILE_ROOT || path.join(dataDir, "flow-browser-profiles"));
  const listed = String(env.FLOW_USER_DATA_DIRS || "").split(/[;\r\n]+/).map((item) => item.trim()).filter(Boolean);
  const numbered = Object.keys(env).filter((name) => /^FLOW_USER_DATA_DIR_\d+$/.test(name) && env[name]).map((name) => Number(name.split("_").at(-1)));
  const configuredCount = env.FLOW_ACCOUNT_COUNT ? Number(env.FLOW_ACCOUNT_COUNT) : 2;
  if (!Number.isInteger(configuredCount) || configuredCount < 1 || configuredCount > 20) throw new Error("FLOW_ACCOUNT_COUNT 必须是 1 到 20 的整数");
  const count = Math.max(configuredCount, listed.length, ...numbered);
  if (count > 20) throw new Error("Flow 持久浏览器资料最多支持 20 个账号");
  const dirs = Array.from({ length: count }, (_, index) => path.resolve(
    (index === 0 ? env.FLOW_USER_DATA_DIR : null) || env[`FLOW_USER_DATA_DIR_${index + 1}`] || listed[index] || path.join(profileRoot, `account-${index + 1}`),
  ));
  const identity = (item) => process.platform === "win32" ? item.toLowerCase() : item;
  if (new Set(dirs.map(identity)).size !== dirs.length) throw new Error("每个 Flow 账号必须使用不同的持久浏览器资料目录");
  env.FLOW_PROFILE_ROOT = profileRoot;
  env.FLOW_USER_DATA_DIR = dirs[0];
  for (let index = 1; index < dirs.length; index++) env[`FLOW_USER_DATA_DIR_${index + 1}`] = dirs[index];
  delete env.FLOW_USER_DATA_DIR_1;
  env.FLOW_USER_DATA_DIRS = dirs.join(";");
  env.FLOW_ACCOUNT_COUNT = String(count);
  env.FLOW_PERSISTENT_PROFILES = "1";
  return { dataDir, profileRoot, dirs };
}

// The reusable session library predates multi-profile mode and caps its own
// config at two accounts. Adapt its exported config without modifying it.
export function applyProfileConfiguration(config, profiles) {
  config.sessionAccountCount = profiles.dirs.length;
  config.sessionSources = profiles.dirs.map(() => null);
  config.userDataDirs = [...profiles.dirs];
  config.profileRoot = profiles.profileRoot;
  config.persistentProfiles = true;
  config.cookiesFile = undefined;
  config.storageStatePath = undefined;
  config.cookiesFiles = profiles.dirs.map(() => undefined);
  config.storageStatePaths = profiles.dirs.map(() => undefined);
}

export function nativeBrowserRunning(native) {
  return Boolean(native && native.exitCode === null && native.signalCode == null);
}

export async function waitForNativeBrowserExit(native, timeoutMs = 10000) {
  if (!nativeBrowserRunning(native)) return;
  await new Promise((resolve, reject) => {
    const finish = (error) => {
      clearTimeout(timer);
      native.removeListener("exit", exited);
      if (error) reject(error); else resolve();
    };
    const exited = () => finish();
    const timer = setTimeout(() => finish(new Error("专用 Chrome 尚未实际退出，保留单浏览器占用，不会启动另一个浏览器")), timeoutMs);
    native.once("exit", exited);
    if (!nativeBrowserRunning(native)) finish();
  });
}

export function profileOnlyBrowserClass(FlowBrowser, accountCount = 2, capacity = null) {
  return class ProfileOnlyFlowBrowser extends FlowBrowser {
    canAcquireCapacity() { return !capacity || capacity.canAcquire(this); }
    async acquireCapacity() { await capacity?.acquire(this); }
    async ensurePage(options) {
      await this.acquireCapacity();
      try { return await super.ensurePage(options); }
      catch (error) { await this.close(); throw error; }
    }
    async beginInteractiveLogin(options) {
      if (this.openingManualLogin || this.manualOperationPending || this.stage === "interactive_login") throw new Error("该账号已经在打开或使用人工登录窗口");
      this.openingManualLogin = true;
      try {
        await this.acquireCapacity();
        return await super.beginInteractiveLogin(options);
      }
      catch (error) {
        await this.close();
        await waitForNativeBrowserExit(this.interactiveLogin?.nativeBrowser);
        capacity?.release(this);
        throw error;
      }
      finally { this.openingManualLogin = false; }
    }
    async finishInteractiveLogin() {
      if (this.openingManualLogin || this.manualOperationPending) throw new Error("该账号的登录操作正在处理中");
      this.manualOperationPending = true;
      try {
        const native = this.interactiveLogin?.nativeBrowser;
        if (nativeBrowserRunning(native)) throw new Error("请先关闭专用 Chrome 窗口（不要退出账号），再验证；不会另开浏览器");
        return await super.finishInteractiveLogin();
      } finally { this.manualOperationPending = false; }
    }
    async cancelInteractiveLogin() {
      if (this.openingManualLogin || this.manualOperationPending) throw new Error("该账号的登录操作正在处理中");
      this.manualOperationPending = true;
      try { return await super.cancelInteractiveLogin(); }
      finally { this.manualOperationPending = false; }
    }
    async close() {
      // The legacy helper suppresses close failures. Do not free the global
      // permit on an unconfirmed shutdown, otherwise a second Chrome can open.
      await this.context?.close();
      if (this.browser?.isConnected?.()) await this.browser.close();
      await super.close();
      if (!this.openingManualLogin) {
        await waitForNativeBrowserExit(this.interactiveLogin?.nativeBrowser);
        capacity?.release(this);
      }
    }
    async loadSessionStates() { return Array(accountCount).fill(null); }
    async currentSessionState() { return null; }
    async applySessionSeedIfNeeded() { return false; }
  };
}
