/** Pure request checks: browser origins must never acquire local Flow actions. */
const LOOPBACK_ADDRESSES = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);
const LOOPBACK_NAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

function failure(status, code, message) {
  return { status, code, message };
}

function localHost(value) {
  if (typeof value !== "string") return false;
  const match = /^(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?$/i.exec(value);
  return Boolean(match && (!match[2] || (Number(match[2]) >= 1 && Number(match[2]) <= 65535)));
}

function localOrigin(value) {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value)) return false;
  try {
    const origin = new URL(value);
    return LOOPBACK_NAMES.has(origin.hostname.toLowerCase()) && !origin.username && !origin.password &&
      origin.pathname === "/" && !origin.search && !origin.hash;
  } catch { return false; }
}

export function flowRequestPolicy(request) {
  const headers = request.headers || {};
  if (!LOOPBACK_ADDRESSES.has(request.socket?.remoteAddress)) {
    return failure(403, "loopback_only", "Local Flow browser control only");
  }
  if (!localHost(headers.host)) {
    return failure(403, "invalid_host", "Flow requires a loopback Host header");
  }
  // CLI/Python clients have no Origin. Explicit opaque origins, including
  // file:// pages (Origin: null), must not gain access to local credentials.
  if (headers.origin !== undefined && !localOrigin(headers.origin)) {
    return failure(403, "invalid_origin", "Cross-origin Flow browser control is not allowed");
  }
  const site = headers["sec-fetch-site"];
  if (site !== undefined && !["same-origin", "same-site", "none"].includes(site)) {
    return failure(403, "cross_site_request", "Cross-site Flow browser control is not allowed");
  }
  let pathname;
  try { pathname = new URL(request.url, "http://127.0.0.1").pathname; }
  catch { return failure(400, "invalid_url", "Invalid Flow request URL"); }
  if (request.method === "POST" && pathname === "/v1/videos") {
    const contentType = headers["content-type"];
    if (typeof contentType !== "string" || contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      return failure(415, "json_required", "Video creation requires application/json");
    }
  }
  return null;
}
