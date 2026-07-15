const DEFAULT_READ_ORIGIN = "https://read.cathedral.computer";
const DEFAULT_SUBMIT_ORIGIN = "https://submit.cathedral.computer";
const DEFAULT_WEIGHTS_ORIGIN = "";

const LEGACY_PREFIX = "/api/cathedral";

const READ_GET_PATHS = new Set([
  "/health",
  "/health/live",
  "/health/ready",
  "/.well-known/cathedral-jwks.json",
  "/v1/synthetic-boolean/active-challenges",
  "/v1/synthetic-boolean/challenge-broadcast",
  "/v1/synthetic-boolean/current-challenge",
  "/v1/synthetic-boolean/per-miner/status",
  "/v1/synthetic-boolean/per-miner/summary",
  "/v1/validator/weights/next",
  "/v1/leaderboard/recent",
  "/v1/leaderboard/top",
  "/v1/leaderboard/explain",
]);

const READ_HEALTH_PATHS = new Set([
  "/health",
  "/health/live",
  "/health/ready",
  "/.well-known/cathedral-jwks.json",
]);

const READ_GET_PREFIXES = [
  "/v1/audit-scanner/",
];

const SUBMIT_GET_PATHS = new Set([
  "/v1/synthetic-boolean/active-cnf",
  "/v1/synthetic-boolean/per-miner/challenges",
  "/v1/synthetic-boolean/per-miner/cnf",
]);

const SUBMIT_GET_PREFIXES = [
  "/v1/challenges/",
];

const SUBMIT_POST_PATHS = new Set([
  "/v1/agents/submit",
]);

const CACHE_POLICIES = new Map([
  ["/v1/synthetic-boolean/active-challenges", { freshTtl: 5, edgeTtl: 900, swr: true, params: [] }],
  ["/v1/synthetic-boolean/challenge-broadcast", { freshTtl: 5, edgeTtl: 900, swr: true, params: [] }],
  ["/v1/synthetic-boolean/current-challenge", { freshTtl: 5, edgeTtl: 300, swr: true, params: ["tier", "difficulty"] }],
  ["/v1/synthetic-boolean/per-miner/summary", { freshTtl: 5, edgeTtl: 30, swr: true, params: ["limit"] }],
  ["/v1/validator/weights/next", { freshTtl: 15, edgeTtl: 300, swr: true, params: [] }],
  ["/v1/leaderboard/recent", { freshTtl: 5, edgeTtl: 300, swr: true, params: ["limit", "since", "since_ran_at", "since_id"] }],
  ["/v1/leaderboard/top", { freshTtl: 15, edgeTtl: 90, swr: true, params: ["window", "view"] }],
  ["/v1/leaderboard/explain", { freshTtl: 10, edgeTtl: 60, swr: true, params: ["miner_hotkey", "uid"] }],
]);

function envUrl(env, key, fallback) {
  const value = env && typeof env[key] === "string" ? env[key].trim() : "";
  return value || fallback;
}

export function canonicalPath(pathname) {
  let path = pathname;
  if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
  if (path === LEGACY_PREFIX) return "/";
  if (path.startsWith(`${LEGACY_PREFIX}/`)) {
    return path.slice(LEGACY_PREFIX.length) || "/";
  }
  return path;
}

function hasPrefix(pathname, prefixes) {
  return prefixes.some((prefix) => pathname.startsWith(prefix));
}

export function classifyRequest(method, pathname) {
  const path = canonicalPath(pathname);
  if (method === "GET" || method === "HEAD") {
    if (READ_GET_PATHS.has(path) || hasPrefix(path, READ_GET_PREFIXES)) {
      return { role: "read", path };
    }
    if (SUBMIT_GET_PATHS.has(path) || hasPrefix(path, SUBMIT_GET_PREFIXES)) {
      return { role: "submit", path };
    }
  }
  if (method === "POST" && SUBMIT_POST_PATHS.has(path)) {
    return { role: "submit", path };
  }
  return { role: "none", path };
}

function isCacheableRead(request, path) {
  if (request.method !== "GET") return false;
  if (path === "/v1/synthetic-boolean/per-miner/status") return false;
  if (path.startsWith("/v1/audit-scanner/")) return false;
  if (request.headers.has("authorization")) return false;
  if (request.headers.has("x-cathedral-signature")) return false;
  return CACHE_POLICIES.has(path);
}

export function cachePolicyForPath(path) {
  return CACHE_POLICIES.get(path) || null;
}

export function normalizedCacheKeyUrl(requestUrl, path) {
  const policy = cachePolicyForPath(path);
  const url = new URL(requestUrl);
  const source = new URL(requestUrl);
  url.pathname = path;
  url.search = "";
  for (const param of policy ? policy.params : []) {
    const values = source.searchParams.getAll(param);
    for (const value of values.sort()) {
      url.searchParams.append(param, value);
    }
  }
  return url.toString();
}

export function unsupportedCacheQueryParams(requestUrl, path) {
  const policy = cachePolicyForPath(path);
  if (!policy) return [];
  if (policy.params.length === 0) return [];
  const allowed = new Set(policy.params);
  const params = new URL(requestUrl).searchParams;
  return [...new Set([...params.keys()].filter((key) => !allowed.has(key)))].sort();
}

function cacheControlForPolicy(policy) {
  const clientTtl = Math.max(0, Math.min(2, policy.freshTtl));
  return `public, max-age=${clientTtl}, s-maxage=${policy.edgeTtl}`;
}

export function originAllowsEdgeStore(response) {
  const cacheControl = (response.headers.get("Cache-Control") || "").toLowerCase();
  if (
    cacheControl.includes("private") ||
    cacheControl.includes("no-store") ||
    cacheControl.includes("no-cache")
  ) {
    return false;
  }
  const vary = response.headers.get("Vary");
  if (!vary) return true;
  return vary
    .split(",")
    .map((part) => part.trim().toLowerCase())
    .filter(Boolean)
    .every((part) => part === "accept" || part === "accept-encoding");
}

function cloneHeaders(headers) {
  const out = new Headers(headers);
  out.delete("host");
  return out;
}

export function originRequest(request, originBase, options = {}) {
  const incoming = new URL(request.url);
  const origin = new URL(originBase);
  const path = canonicalPath(incoming.pathname);
  if (options.preserveOriginPath) {
    const basePath = origin.pathname.endsWith("/") ? origin.pathname.slice(0, -1) : origin.pathname;
    origin.pathname = `${basePath}${path}`;
  } else {
    origin.pathname = path;
  }
  origin.search = options.search !== undefined ? options.search : incoming.search;
  const headers = cloneHeaders(request.headers);
  if (options.hostHeader) {
    headers.set("Host", options.hostHeader);
  }
  if (options.acceptHeader) {
    headers.set("Accept", options.acceptHeader);
  }
  if (options.userAgent) {
    headers.set("User-Agent", options.userAgent);
  }
  const init = {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    redirect: "manual",
  };
  if (init.body !== undefined) {
    init.duplex = "half";
  }
  return new Request(origin.toString(), init);
}

function responseWithEdgeHeaders(response, headers, options = {}) {
  const out = new Response(response.body, response);
  for (const [key, value] of Object.entries(headers)) {
    out.headers.set(key, value);
  }
  if (options.forceCors || !out.headers.has("Access-Control-Allow-Origin")) {
    out.headers.set("Access-Control-Allow-Origin", "*");
  }
  return out;
}

function preflightResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
      "Access-Control-Allow-Headers": [
        "authorization",
        "content-type",
        "x-cathedral-hotkey",
        "x-cathedral-signature",
        "x-cathedral-submitted-at",
      ].join(", "),
      "Access-Control-Max-Age": "600",
      "Cache-Control": "public, max-age=600",
    },
  });
}

function timeoutSignal(ms) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(ms);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
}

function fetchWithTimeout(request, ms, options = {}) {
  const init = { signal: timeoutSignal(ms) };
  if (options.resolveOverride) {
    init.cf = { resolveOverride: options.resolveOverride };
  }
  return fetch(request, init);
}

function originTimeoutMs(env, key, fallback) {
  const value = Number(env && env[key]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envBool(env, key, fallback = false) {
  const raw = env && env[key];
  if (typeof raw === "boolean") return raw;
  if (raw === undefined || raw === null || raw === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(raw).trim().toLowerCase());
}

function envNumber(env, key, fallback) {
  const value = Number(env && env[key]);
  return Number.isFinite(value) ? value : fallback;
}

export function originTimeoutForRoute(env, role, path, cacheable = false) {
  if (role === "submit") {
    return originTimeoutMs(env, "SUBMIT_ORIGIN_TIMEOUT_MS", 10000);
  }
  if (role === "read" && cacheable) {
    return originTimeoutMs(env, "READ_ORIGIN_TIMEOUT_MS", 4500);
  }
  if (role === "read" && READ_HEALTH_PATHS.has(path)) {
    return originTimeoutMs(env, "READ_HEALTH_ORIGIN_TIMEOUT_MS", 9000);
  }
  return originTimeoutMs(env, "READ_ORIGIN_TIMEOUT_MS", 4500);
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function shouldMirrorV1Submit(env, route, request) {
  if (!envBool(env, "SHADOW_V1_MIRROR_ENABLED", false)) return false;
  if (!route || route.role !== "submit" || route.path !== "/v1/agents/submit") return false;
  if (request.method !== "POST") return false;
  if (!envUrl(env, "SHADOW_V1_MIRROR_ORIGIN", "")) return false;
  const pct = Math.max(0, Math.min(100, envNumber(env, "SHADOW_V1_MIRROR_SAMPLE_PERCENT", 100)));
  if (pct <= 0) return false;
  if (pct >= 100) return true;
  return Math.random() * 100 < pct;
}

function textBytes(text) {
  return new TextEncoder().encode(text || "").length;
}

function randomRequestId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function fullShadowV1MirrorRequest(request, env) {
  const mirrorBase = envUrl(env, "SHADOW_V1_MIRROR_ORIGIN", "");
  const url = new URL(mirrorBase);
  url.pathname = "/v2/shadow/v1/agents/submit";
  url.search = "";
  const headers = cloneHeaders(request.headers);
  headers.set("X-Cathedral-Shadow-Source", envUrl(env, "SHADOW_V1_MIRROR_SOURCE", "edge-mirror-v1"));
  const init = {
    method: "POST",
    headers,
    body: request.body,
    redirect: "manual",
  };
  init.duplex = "half";
  return new Request(url.toString(), init);
}

async function metaShadowV1MirrorRequest(request, env) {
  const edgeReceivedAt = new Date().toISOString();
  const mirrorBase = envUrl(env, "SHADOW_V1_MIRROR_ORIGIN", "");
  const url = new URL(mirrorBase);
  url.pathname = "/v2/shadow/v1/agents/submit/meta";
  url.search = "";

  const source = envUrl(env, "SHADOW_V1_MIRROR_SOURCE", "edge-mirror-v1-meta");
  const contentType = request.headers.get("content-type") || "";
  const contentLength = Number(request.headers.get("content-length") || "0");
  const bodyText = await request.text();
  const payload = {
    schema: "cathedral.v2.shadow_v1_submit_meta.v1",
    request_id: randomRequestId(),
    source,
    edge_received_at_iso: edgeReceivedAt,
    miner_hotkey: request.headers.get("x-cathedral-hotkey") || "",
    submitted_at: request.headers.get("x-cathedral-submitted-at") || "",
    signature_present: request.headers.has("x-cathedral-signature"),
    content_type: contentType,
    original_content_length: Number.isFinite(contentLength) && contentLength > 0 ? contentLength : 0,
    original_body_bytes: textBytes(bodyText),
    dimacs_solution_bytes: 0,
    field_count: 0,
    card_id: "",
    challenge_id: "",
    parse_error: "",
  };

  try {
    const params = new URLSearchParams(bodyText);
    payload.field_count = Array.from(params.keys()).length;
    payload.card_id = params.get("card_id") || "";
    payload.challenge_id = params.get("challenge_id") || "";
    payload.submitted_at = params.get("submitted_at") || payload.submitted_at;
    payload.dimacs_solution_bytes = textBytes(params.get("dimacs_solution") || "");
  } catch (error) {
    payload.parse_error = String(error && error.message || error).slice(0, 256);
  }

  const headers = new Headers();
  headers.set("Content-Type", "application/json");
  headers.set("X-Cathedral-Shadow-Source", source);
  if (payload.miner_hotkey) headers.set("X-Cathedral-Hotkey", payload.miner_hotkey);
  return new Request(url.toString(), {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    redirect: "manual",
  });
}

async function shadowV1MirrorRequest(request, env) {
  const mode = envUrl(env, "SHADOW_V1_MIRROR_MODE", "full").toLowerCase();
  if (mode === "meta" || mode === "metadata" || mode === "metadata_only") {
    return metaShadowV1MirrorRequest(request, env);
  }
  return fullShadowV1MirrorRequest(request, env);
}

async function mirrorV1Submit(request, env) {
  try {
    const timeoutMs = originTimeoutMs(env, "SHADOW_V1_MIRROR_TIMEOUT_MS", 2500);
    await fetchWithTimeout(await shadowV1MirrorRequest(request, env), timeoutMs);
  } catch {
    // Shadow mirroring must never affect the authoritative submit response.
  }
}

export function cachedFresh(response, now = nowSeconds()) {
  const until = Number(response.headers.get("x-cathedral-edge-fresh-until") || "0");
  return Number.isFinite(until) && until > now;
}

export async function responseIsVisibilityWarming(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) return false;
  try {
    const body = await response.clone().json();
    if (!body || typeof body !== "object") return false;
    if (body.visibility_cache_status === "warming" || body.data_status === "warming") {
      return true;
    }
    const visibility = body.visibility || {};
    if (visibility.current_signed_weight_status === "warming") return true;
    const sources = body.sources || visibility.sources || {};
    return Object.values(sources).some((source) => source && source.status === "warming");
  } catch {
    return false;
  }
}

async function fetchReadThroughCache(
  request,
  originBase,
  ctx,
  path,
  timeoutMs,
  originHost = "",
  resolveOverride = "",
  originRequestOptions = {},
) {
  const unsupportedParams = unsupportedCacheQueryParams(request.url, path);
  if (unsupportedParams.length) {
    return Response.json(
      {
        error: "unsupported_cache_query_param",
        path,
        unsupported_params: unsupportedParams,
      },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }

  const cache = caches.default;
  const policy = cachePolicyForPath(path);
  const normalizedUrl = normalizedCacheKeyUrl(request.url, path);
  const cacheKey = new Request(normalizedCacheKeyUrl(request.url, path), {
    method: "GET",
    headers: { accept: request.headers.get("accept") || "application/json" },
  });
  const originReq = originRequest(request, originBase, {
    search: originRequestOptions.search !== undefined ? originRequestOptions.search : new URL(normalizedUrl).search,
    hostHeader: originHost,
    preserveOriginPath: originRequestOptions.preserveOriginPath,
    acceptHeader: originRequestOptions.acceptHeader,
    userAgent: originRequestOptions.userAgent,
  });
  const cached = await cache.match(cacheKey);
  const cachedCanServe = cached && cached.status >= 200 && cached.status < 500;
  if (cachedCanServe && cachedFresh(cached)) {
    return responseWithEdgeHeaders(cached, { "X-Cathedral-Edge-Cache": "HIT" });
  }

  if (cachedCanServe && policy && policy.swr !== false) {
    ctx.waitUntil(refreshCachedRead(cache, cacheKey, originReq, policy, timeoutMs, resolveOverride));
    return responseWithEdgeHeaders(cached, { "X-Cathedral-Edge-Cache": "STALE-REFRESH" });
  }

  let originResp;
  try {
    originResp = await fetchWithTimeout(originReq, timeoutMs, { resolveOverride });
  } catch (error) {
    if (cachedCanServe) {
      return responseWithEdgeHeaders(cached, {
        "X-Cathedral-Edge-Cache": "STALE",
        "Warning": `110 - "origin fetch failed: ${String(error && error.message || error)}"`,
      });
    }
    return Response.json(
      { error: "read_origin_unavailable", detail: String(error && error.message || error) },
      { status: 504, headers: { "Cache-Control": "no-store" } },
    );
  }

  const outHeaders = {
    "X-Cathedral-Edge-Cache": cached ? "REFRESH" : "MISS",
  };
  const shouldStore = (
    policy &&
    request.method === "GET" &&
    originResp.status === 200 &&
    originAllowsEdgeStore(originResp) &&
    !(await responseIsVisibilityWarming(originResp))
  );
  if (!shouldStore) {
    return responseWithEdgeHeaders(originResp, outHeaders);
  }

  const freshUntil = nowSeconds() + policy.freshTtl;
  const toStore = responseWithEdgeHeaders(originResp.clone(), {
    "Cache-Control": cacheControlForPolicy(policy),
    "X-Cathedral-Edge-Fresh-Until": String(freshUntil),
  }, { forceCors: true });
  toStore.headers.delete("Set-Cookie");
  ctx.waitUntil(cache.put(cacheKey, toStore.clone()));
  return responseWithEdgeHeaders(toStore, outHeaders);
}

async function refreshCachedRead(cache, cacheKey, originReq, policy, timeoutMs, resolveOverride = "") {
  try {
    const originResp = await fetchWithTimeout(originReq, timeoutMs, { resolveOverride });
    if (originResp.status !== 200 || !originAllowsEdgeStore(originResp)) return;
    if (await responseIsVisibilityWarming(originResp)) return;
    const freshUntil = nowSeconds() + policy.freshTtl;
    const toStore = responseWithEdgeHeaders(originResp.clone(), {
      "Cache-Control": cacheControlForPolicy(policy),
      "X-Cathedral-Edge-Fresh-Until": String(freshUntil),
    }, { forceCors: true });
    toStore.headers.delete("Set-Cookie");
    await cache.put(cacheKey, toStore);
  } catch {
    // Keep serving the existing stale object until its cache TTL expires.
  }
}

export async function handleRequest(request, env = {}, ctx = { waitUntil() {} }) {
  if (request.method === "OPTIONS") {
    return preflightResponse();
  }

  const url = new URL(request.url);
  const route = classifyRequest(request.method, url.pathname);
  if (route.role === "none") {
    return Response.json(
      { error: "route_not_served_by_cathedral_edge", path: route.path },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }

  const weightsOrigin = route.role === "read" && route.path === "/v1/validator/weights/next"
    ? envUrl(env, "WEIGHTS_ORIGIN", DEFAULT_WEIGHTS_ORIGIN)
    : "";
  const originBase = weightsOrigin || (route.role === "read"
    ? envUrl(env, "READ_ORIGIN", DEFAULT_READ_ORIGIN)
    : envUrl(env, "SUBMIT_ORIGIN", DEFAULT_SUBMIT_ORIGIN));
  const originHost = weightsOrigin
    ? envUrl(env, "WEIGHTS_ORIGIN_HOST", "")
    : (route.role === "read"
      ? envUrl(env, "READ_ORIGIN_HOST", "")
      : envUrl(env, "SUBMIT_ORIGIN_HOST", ""));
  const resolveOverride = weightsOrigin
    ? envUrl(env, "WEIGHTS_ORIGIN_RESOLVE_OVERRIDE", "")
    : (route.role === "read"
      ? envUrl(env, "READ_ORIGIN_RESOLVE_OVERRIDE", "")
      : envUrl(env, "SUBMIT_ORIGIN_RESOLVE_OVERRIDE", ""));
  const originRequestOptions = weightsOrigin ? {
    preserveOriginPath: true,
    search: envUrl(env, "WEIGHTS_ORIGIN_SEARCH", "?ref=main"),
    acceptHeader: envUrl(env, "WEIGHTS_ORIGIN_ACCEPT", "application/vnd.github.raw"),
    userAgent: envUrl(env, "WEIGHTS_ORIGIN_USER_AGENT", "cathedral-edge-router"),
  } : {};
  const mirrorSource = shouldMirrorV1Submit(env, route, request) ? request.clone() : null;
  const originReq = originRequest(mirrorSource ? request.clone() : request, originBase, {
    hostHeader: originHost,
    ...originRequestOptions,
  });
  if (mirrorSource) {
    ctx.waitUntil(mirrorV1Submit(mirrorSource, env));
  }

  if (route.role === "read" && isCacheableRead(request, route.path)) {
    return fetchReadThroughCache(
      request,
      originBase,
      ctx,
      route.path,
      originTimeoutForRoute(env, route.role, route.path, true),
      originHost,
      resolveOverride,
      originRequestOptions,
    );
  }

  let resp;
  try {
    resp = await fetchWithTimeout(
      originReq,
      originTimeoutForRoute(env, route.role, route.path),
      { resolveOverride },
    );
  } catch (error) {
    return Response.json(
      { error: `${route.role}_origin_unavailable`, detail: String(error && error.message || error) },
      { status: 504, headers: { "Cache-Control": "no-store" } },
    );
  }
  return responseWithEdgeHeaders(resp, {
    "X-Cathedral-Edge-Cache": "BYPASS",
    "Cache-Control": resp.headers.get("Cache-Control") || "no-store",
  });
}

export default {
  fetch: handleRequest,
};
