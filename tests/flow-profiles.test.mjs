import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { configureProfileEnvironment, applyProfileConfiguration, profileOnlyBrowserClass } from "../scripts/flow-ai/profiles.mjs";
import { chooseAvailableWorker } from "../scripts/flow-ai/account-pool.mjs";

test("profile-only configuration discards every legacy authentication export before config import", () => {
  const env = { QQ_AI_DATA_DIR: path.resolve("test-data"), FLOW_PERSISTENT_PROFILES: "0" };
  for (const name of ["FLOW_COOKIES_FILE", "FLOW_COOKIES_FILE_1", "FLOW_COOKIES_FILE_2", "FLOW_COOKIES_FILES", "FLOW_STORAGE_STATE", "FLOW_STORAGE_STATE_1", "FLOW_STORAGE_STATE_2", "FLOW_STORAGE_STATES"]) env[name] = "never-read-this-file";
  const result = configureProfileEnvironment(env);
  assert.equal(env.FLOW_PERSISTENT_PROFILES, "1");
  assert.equal(Object.keys(env).some((name) => /FLOW_(COOKIES|STORAGE_STATE)/.test(name)), false);
  assert.deepEqual(result.dirs, [1, 2].map((n) => path.join(env.QQ_AI_DATA_DIR, "flow-browser-profiles", `account-${n}`)));
});

test("five accounts retain existing paths and expand the legacy two-account config", async () => {
  const existing = [path.resolve("existing-1"), path.resolve("existing-2")];
  const env = { QQ_AI_DATA_DIR: path.resolve("test-data"), FLOW_ACCOUNT_COUNT: "5", FLOW_USER_DATA_DIR: existing[0], FLOW_USER_DATA_DIR_2: existing[1], FLOW_COOKIES_FILE_5: "must-not-read" };
  const profiles = configureProfileEnvironment(env);
  assert.equal(profiles.dirs.length, 5);
  assert.deepEqual(profiles.dirs.slice(0, 2), existing);
  assert.equal(profiles.dirs[4], path.join(profiles.profileRoot, "account-5"));
  assert.equal(env.FLOW_COOKIES_FILE_5, undefined);
  assert.equal(configureProfileEnvironment(env).dirs.length, 5, "normalization must be idempotent");
  const config = { sessionAccountCount: 2, cookiesFile: "old", storageStatePath: "old" };
  applyProfileConfiguration(config, profiles);
  assert.equal(config.sessionAccountCount, 5);
  assert.deepEqual(config.userDataDirs, profiles.dirs);
  assert.deepEqual(config.sessionSources, Array(5).fill(null));
  assert.equal(config.cookiesFile, undefined);
  const Browser = profileOnlyBrowserClass(class {}, 5);
  assert.deepEqual(await new Browser().loadSessionStates(), Array(5).fill(null));
});

test("numbered and listed profile slots infer account count and reject duplicates in later slots", () => {
  const root = path.resolve("profiles");
  assert.equal(configureProfileEnvironment({ FLOW_PROFILE_ROOT: root, FLOW_USER_DATA_DIR_5: path.join(root, "fifth") }).dirs.length, 5);
  assert.equal(configureProfileEnvironment({ FLOW_USER_DATA_DIRS: [1, 2, 3, 4, 5].map(n => path.join(root, String(n))).join(";") }).dirs.length, 5);
  assert.throws(() => configureProfileEnvironment({ FLOW_ACCOUNT_COUNT: "5", FLOW_USER_DATA_DIR_3: "same", FLOW_USER_DATA_DIR_5: "same" }), /不同/);
  for (const value of ["0", "21", "2.5", "NaN"]) assert.throws(() => configureProfileEnvironment({ FLOW_ACCOUNT_COUNT: value }), /整数/);
});

test("new unauthenticated profiles never crowd out usable accounts or control manual login", () => {
  const workers = Array.from({ length: 5 }, () => ({ load: () => 0, stats: () => ({ stage: "idle" }) }));
  const available = [true, true, false, false, false];
  assert.equal(chooseAvailableWorker(workers, 2, null, available), 0);
  assert.equal(chooseAvailableWorker(workers, 1, 0, available), 1);
  workers[0].stats = () => ({ stage: "interactive_login" });
  assert.equal(chooseAvailableWorker(workers, 0, null, available), 1);
  assert.throws(() => chooseAvailableWorker(workers, 0, null, Array(5).fill(false)), /没有可用/);
  assert.equal(chooseAvailableWorker(workers, 2, null, [true, true, true, false, false]), 2);
  workers[0].stats = () => ({ stage: "idle" });
  assert.equal(chooseAvailableWorker(workers, 0, null, [undefined, undefined, undefined, true, undefined]), 3, "known-ready account must precede unchecked profiles");
  assert.notEqual(chooseAvailableWorker(workers, 0, 3, [undefined, undefined, undefined, true, undefined]), 3, "a failed attempt still rotates to another unchecked account");
});

test("configured persistent paths stay stable and duplicate account directories are rejected", () => {
  const dirs = [path.resolve("existing-account-1"), path.resolve("existing-account-2")];
  const env = { FLOW_USER_DATA_DIR: dirs[0], FLOW_USER_DATA_DIR_2: dirs[1] };
  assert.deepEqual(configureProfileEnvironment(env).dirs, dirs);
  assert.throws(() => configureProfileEnvironment({ FLOW_USER_DATA_DIR: dirs[0], FLOW_USER_DATA_DIR_2: dirs[0] }), /不同/);
  assert.deepEqual(configureProfileEnvironment({ FLOW_USER_DATA_DIRS: dirs.join(";") }).dirs, dirs);
});

test("profile browser cannot load or inject authentication seeds even when explicitly passed one", async () => {
  class LegacyBrowser {
    async loadSessionStates() { throw new Error("legacy file read"); }
    async currentSessionState() { throw new Error("legacy state read"); }
    async applySessionSeedIfNeeded() { throw new Error("legacy cookie injection"); }
  }
  const Browser = profileOnlyBrowserClass(LegacyBrowser);
  const browser = new Browser();
  assert.deepEqual(await browser.loadSessionStates(), [null, null]);
  assert.equal(await browser.currentSessionState(), null);
  assert.equal(await browser.applySessionSeedIfNeeded({ state: { cookies: ["must not import"] } }), false);
});
