import test from "node:test";
import assert from "node:assert/strict";
import { flowRequestPolicy } from "../scripts/flow-ai/request-policy.mjs";

function request(headers = {}, extra = {}) {
  return { method: "GET", url: "/healthz", socket: { remoteAddress: "127.0.0.1" }, headers: { host: "127.0.0.1:8787", ...headers }, ...extra };
}

test("local CLI and Python clients remain allowed without Origin or fetch metadata", () => {
  for (const host of ["127.0.0.1:8787", "localhost:8787", "LOCALHOST", "[::1]:8787"]) {
    assert.equal(flowRequestPolicy(request({ host })), null);
  }
  for (const remoteAddress of ["127.0.0.1", "::1", "::ffff:127.0.0.1"]) {
    assert.equal(flowRequestPolicy(request({}, { socket: { remoteAddress } })), null);
  }
  assert.equal(flowRequestPolicy(request({ "content-type": "application/json; charset=utf-8" }, { method: "POST", url: "/v1/videos" })), null);
  for (const url of ["/v1/flow/accounts/1/login", "/v1/flow/accounts/1/check"]) {
    assert.equal(flowRequestPolicy(request({}, { method: "POST", url })), null);
  }
});

test("foreign, malformed and missing Host cannot access Flow through loopback sockets", () => {
  for (const host of [undefined, "evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "127.0.0.1@evil.example", "127.0.0.1:0", "localhost:65536", "localhost/path", "localhost, evil.example", ["localhost", "evil.example"]]) {
    assert.equal(flowRequestPolicy(request({ host }))?.code, "invalid_host");
  }
  assert.equal(flowRequestPolicy(request({}, { socket: { remoteAddress: "192.168.1.2" } }))?.code, "loopback_only");
});

test("browser requests from local HTTP origins are accepted", () => {
  for (const origin of ["http://127.0.0.1:8765", "http://localhost:8787", "https://localhost", "http://[::1]:8765"]) {
    for (const site of [undefined, "same-origin", "same-site", "none"]) {
      assert.equal(flowRequestPolicy(request({ origin, "sec-fetch-site": site })), null);
    }
  }
});

test("foreign and opaque Origin is rejected for every endpoint, including bodyless actions", () => {
  for (const origin of ["null", "", "file://", "chrome-extension://id", "https://evil.example", "https://localhost.evil.example", "http://localhost@evil.example", "http://user:password@localhost", "http://localhost/path", "http://localhost?x=1", "http://localhost#x", ["http://localhost"]]) {
    for (const url of ["/healthz", "/v1/videos", "/v1/flow/accounts/1/login", "/v1/flow/accounts/1/login/cancel", "/v1/flow/accounts/1/check"]) {
      assert.equal(flowRequestPolicy(request({ origin, "content-type": "application/json" }, { method: "POST", url }))?.code, "invalid_origin");
    }
  }
});

test("cross-site fetch metadata is rejected even without Origin", () => {
  for (const site of ["cross-site", "", "unexpected", ["same-origin"]]) {
    for (const origin of [undefined, "http://localhost:8765"]) {
      assert.equal(flowRequestPolicy(request({ origin, "sec-fetch-site": site }, { method: "POST", url: "/v1/flow/accounts/1/login" }))?.code, "cross_site_request");
    }
  }
});

test("video creation rejects simple request media types before accepting a body", () => {
  for (const contentType of [undefined, "text/plain", "text/plain; charset=utf-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx", ["application/json"]]) {
    const blocked = flowRequestPolicy(request({ "content-type": contentType }, { method: "POST", url: "/v1/videos?test=1" }));
    assert.equal(blocked?.status, 415);
    assert.equal(blocked?.code, "json_required");
  }
  assert.equal(flowRequestPolicy(request({ "content-type": "Application/JSON" }, { method: "POST", url: "/v1/videos" })), null);
});
