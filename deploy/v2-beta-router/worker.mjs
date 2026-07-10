const ORIGIN_HOST = "sandbox-v2-origin.cathedral.computer";
const ORIGIN_PORT = "8080";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

// Staged reopen: only canary hotkeys reach the origin while the edge-artifact
// read path (issue #363/#372 direction) is being stood up. Everyone else gets
// a fast edge 429 so the 4-vCPU sandbox origin never sees the stampede.
const CANARY_HOTKEYS = new Set([
  "5H1DGfCH5A6sRxiA64xdTG4SSpf7HpXtqGB8bohqnU1MWqv4", // Stitch dogfood miner
  "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", // //Alice dev key (E2E smoke)
]);

const LEGACY_PREFIX = "/api/cathedral";
const V2_GATE_OPEN = "open-v2";
const RECEIPT_PENDING_TTL_SECS = 2;
const RECEIPT_TERMINAL_TTL_SECS = 300;
const SENSITIVE_CACHE_HEADERS = new Set([
  "authorization",
  "cookie",
  "proxy-authorization",
  "set-cookie",
  "x-cathedral-signature",
  "x-cathedral-submitted-at",
  "x-cathedral-submit-token",
  "x-cathedral-submit-token-expires-at",
]);

function stripLegacyPrefix(pathname) {
  return pathname.startsWith(LEGACY_PREFIX) ? pathname.slice(LEGACY_PREFIX.length) : pathname;
}

function isV2MinerPath(url, method) {
  // V2 is the only reopen path. It is staged by default and opens to all only
  // when the deployed worker receives V2_GATE_MODE=open-v2.
  const path = stripLegacyPrefix(url.pathname);
  if (method === "GET") {
    return path === "/v2/synthetic-boolean/per-miner/challenges"
      || path === "/v2/synthetic-boolean/per-miner/cnf"
      || path === "/v2/synthetic-boolean/per-miner/cnf-access";
  }
  if (method === "POST") {
    return path === "/v2/agents/submit-bitset"
      || path === "/v2/agents/submit-manifest"
      || path === "/v2/blobs/solutions";
  }
  return false;
}

function isRetiredV1Path(url, method) {
  // V1 miner and violet paths are retired for this relaunch. They never reach
  // the origin, including canary hotkeys.
  const path = stripLegacyPrefix(url.pathname);
  if (method === "GET") {
    return path === "/v1/synthetic-boolean/per-miner/challenges"
      || path === "/v1/synthetic-boolean/per-miner/cnf"
      || path === "/v1/synthetic-boolean/active-cnf";
  }
  if (method === "POST") {
    return path === "/v1/agents/submit"
      || path === "/v1/external-scores/violet";
  }
  return false;
}

function isV2GateOpen(env) {
  return String(env?.V2_GATE_MODE || "").trim().toLowerCase() === V2_GATE_OPEN;
}

// Percentage ramp: while the gate is staged, V2_OPEN_PERCENT (0-100) admits a
// stable, hotkey-hashed slice of miners. 0/unset keeps pure staged behaviour;
// 100 admits everyone (same as open-v2). A hotkey's bucket never changes, so
// raising the percent only ever ADDS miners - nobody flaps in and out.
function openPercent(env) {
  const n = Number(String(env?.V2_OPEN_PERCENT ?? "").trim());
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, Math.floor(n)));
}

function hotkeyBucket(hotkey) {
  // FNV-1a 32-bit over the hotkey string -> stable bucket 0-99.
  let h = 0x811c9dc5;
  for (let i = 0; i < hotkey.length; i++) {
    h ^= hotkey.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h % 100;
}

function admittedByRamp(hotkey, env) {
  const percent = openPercent(env);
  if (percent <= 0) return false;
  if (percent >= 100) return true;
  return hotkeyBucket(hotkey) < percent;
}

// ---- Edge per-miner rate limit (2026-07-09) -------------------------------
// Infra flood defense, NOT an economic throttle (difficulty is the economic
// throttle). Every reopen wedge so far had the same shape: a synchronized
// fetch/poll stampede saturated the origin's accept queue before any in-app
// guard could engage. This caps each hotkey's request rate AT THE EDGE with a
// token bucket, so the excess burns as fast Cloudflare 429s instead of origin
// sockets. Limits are generous for a legitimate miner (fetch a batch, solve
// for minutes, submit, poll a receipt ~1/s).
//
// Scope note: the bucket map is per Worker isolate, so the true ceiling is
// (configured rate) x (active isolates). A single miner's traffic mostly
// lands on one PoP/isolate, so per-miner enforcement stays roughly honest;
// treat the knobs as order-of-magnitude protection, not precise accounting.
//
// Knobs: V2_EDGE_MINER_RPS (default 3, 0 disables), V2_EDGE_MINER_BURST
// (default 12). Canary hotkeys are exempt so E2E smoke and edge soak keep
// working.
const _edgeBuckets = new Map();
const _EDGE_BUCKET_SWEEP_SIZE = 8192;

function minerRps(env) {
  const n = Number(String(env?.V2_EDGE_MINER_RPS ?? "3").trim());
  if (!Number.isFinite(n) || n < 0) return 3;
  return n;
}

function minerBurst(env) {
  const n = Number(String(env?.V2_EDGE_MINER_BURST ?? "12").trim());
  if (!Number.isFinite(n) || n < 1) return 12;
  return Math.floor(n);
}

function isReceiptPoll(url, method) {
  if (method !== "GET") return false;
  const path = stripLegacyPrefix(url.pathname);
  return path.startsWith("/v2/agents/submit-bitset/receipts/");
}

function receiptIdFromPath(url) {
  const path = stripLegacyPrefix(url.pathname);
  const prefix = "/v2/agents/submit-bitset/receipts/";
  if (!path.startsWith(prefix)) return null;
  const encoded = path.slice(prefix.length);
  if (!encoded || encoded.includes("/")) return null;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return null;
  }
}

function receiptCacheKey(url) {
  // Keep the legacy and canonical routes isolated. Query parameters are not
  // part of receipt identity and the origin handler ignores them.
  return new Request(`${url.origin}${url.pathname}`, { method: "GET" });
}

function hasSensitiveCacheHeaders(headers) {
  for (const name of SENSITIVE_CACHE_HEADERS) {
    if (headers.has(name)) return true;
  }
  return false;
}

async function cacheableReceipt(response, receiptId) {
  if (response.status !== 200 || hasSensitiveCacheHeaders(response.headers)) return null;
  let payload;
  try {
    payload = await response.clone().json();
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object"
      || payload.schema !== "cathedral.v2.submit_bitset_receipt.v1"
      || payload.receipt_id !== receiptId
      || typeof payload.terminal !== "boolean") {
    return null;
  }
  return payload.terminal ? RECEIPT_TERMINAL_TTL_SECS : RECEIPT_PENDING_TTL_SECS;
}

function clientReceiptResponse(response, cacheStatus) {
  const headers = new Headers(response.headers);
  headers.set("cache-control", "no-store");
  headers.set("x-cathedral-v2-beta-cache", cacheStatus);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function isRateLimitedPath(url, method) {
  // Receipt polls are included deliberately: they bypass the staged gate
  // (they are not in isV2MinerPath) and are the highest-RPS miner behaviour
  // observed in every open window.
  return isV2MinerPath(url, method) || isReceiptPoll(url, method);
}

function takeEdgeToken(key, env, nowMs = Date.now()) {
  const rps = minerRps(env);
  if (rps <= 0) return true; // disabled
  const burst = minerBurst(env);
  let b = _edgeBuckets.get(key);
  if (!b) {
    if (_edgeBuckets.size >= _EDGE_BUCKET_SWEEP_SIZE) {
      // Cheap pressure valve: drop buckets idle for >60s so the map cannot
      // grow without bound inside a long-lived isolate.
      for (const [k, v] of _edgeBuckets) {
        if (nowMs - v.last > 60_000) _edgeBuckets.delete(k);
      }
    }
    b = { tokens: burst, last: nowMs };
    _edgeBuckets.set(key, b);
  }
  const elapsedSecs = Math.max(0, (nowMs - b.last) / 1000);
  b.tokens = Math.min(burst, b.tokens + elapsedSecs * rps);
  b.last = nowMs;
  if (b.tokens < 1) return false;
  b.tokens -= 1;
  return true;
}

function edgeRateLimitedResponse() {
  return new Response(JSON.stringify({
    detail: "edge_rate_limited",
    reason: "edge_rate_limited",
    message: "Per-miner edge rate limit exceeded. Slow your fetch/submit/receipt-poll loop and retry.",
    retry_after_seconds: 2,
  }), {
    status: 429,
    headers: {
      "content-type": "application/json",
      "retry-after": "2",
      "cache-control": "no-store",
      "x-cathedral-rejection-reason": "edge_rate_limited",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-rate-limit",
    },
  });
}

function stagedReopenResponse() {
  return new Response(JSON.stringify({
    detail: "v2_beta_staged_reopen",
    reason: "v2_beta_staged_reopen",
    message: "v2-beta is back online in staged mode. Full miner access is being restored via edge-published challenge artifacts. Retry later.",
    retry_after_seconds: 600,
  }), {
    status: 429,
    headers: {
      "content-type": "application/json",
      "retry-after": "600",
      "cache-control": "no-store",
      "x-cathedral-rejection-reason": "v2_beta_staged_reopen",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-staged-reopen",
    },
  });
}

function v1RetiredResponse() {
  return new Response(JSON.stringify({
    detail: "v1_miner_path_retired",
    reason: "v1_miner_path_retired",
    message: "The V1 miner path is retired for this relaunch. Use V2: fetch challenges, fetch CNF, read X-Cathedral-Submit-Token, then POST /v2/agents/submit-bitset.",
  }), {
    status: 410,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-cathedral-rejection-reason": "v1_miner_path_retired",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-v1-retired",
    },
  });
}

function isPerMinerRead(url) {
  const path = stripLegacyPrefix(url.pathname);
  return path === "/v2/synthetic-boolean/per-miner/challenges"
    || path === "/v2/synthetic-boolean/per-miner/cnf"
    || path === "/v2/synthetic-boolean/per-miner/cnf-access"
    || path === "/v1/synthetic-boolean/per-miner/challenges"
    || path === "/v1/synthetic-boolean/per-miner/cnf";
}

function missingRequiredMinerHeaders(request) {
  return !request.headers.get("x-cathedral-hotkey")
    || !request.headers.get("x-cathedral-signature");
}

function edgeValidationResponse() {
  return new Response(JSON.stringify({
    detail: "missing required miner authentication headers",
    required_headers: ["x-cathedral-hotkey", "x-cathedral-signature"],
  }), {
    status: 422,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-preflight",
    },
  });
}

function forwardedHeaders(request) {
  const headers = new Headers(request.headers);
  for (const name of HOP_BY_HOP_HEADERS) headers.delete(name);
  headers.delete("host");
  headers.set("x-forwarded-host", new URL(request.url).host);
  headers.set("x-forwarded-proto", "https");
  headers.set("x-cathedral-v2-beta-router", "cloudflare-worker");
  return headers;
}

export default {
  async fetch(request, env = {}) {
    const incomingUrl = new URL(request.url);
    if (request.method === "GET" && isPerMinerRead(incomingUrl) && missingRequiredMinerHeaders(request)) {
      return edgeValidationResponse();
    }
    if (isRetiredV1Path(incomingUrl, request.method)) {
      return v1RetiredResponse();
    }
    if (isV2MinerPath(incomingUrl, request.method) && !isV2GateOpen(env)) {
      const hotkey = (request.headers.get("x-cathedral-hotkey") || "").trim();
      if (!CANARY_HOTKEYS.has(hotkey) && !admittedByRamp(hotkey, env)) {
        return stagedReopenResponse();
      }
    }
    if (isRateLimitedPath(incomingUrl, request.method)) {
      const hotkey = (request.headers.get("x-cathedral-hotkey") || "").trim();
      if (!CANARY_HOTKEYS.has(hotkey)) {
        const key = hotkey || request.headers.get("cf-connecting-ip") || "anon";
        if (!takeEdgeToken(key, env)) {
          return edgeRateLimitedResponse();
        }
      }
    }

    const receiptId = request.method === "GET" ? receiptIdFromPath(incomingUrl) : null;
    const receiptCache = env.RECEIPT_CACHE || globalThis.caches?.default;
    const cacheKey = receiptId && receiptCache ? receiptCacheKey(incomingUrl) : null;
    if (cacheKey) {
      try {
        const cached = await receiptCache.match(cacheKey);
        if (cached) return clientReceiptResponse(cached, "HIT");
      } catch {
        // Cache availability must never become receipt availability.
      }
    }

    const originUrl = new URL(request.url);
    if (request.method === "GET" && isPerMinerRead(originUrl)) {
      const requestedLimit = Number(originUrl.searchParams.get("limit") || "0");
      if (!Number.isFinite(requestedLimit) || requestedLimit <= 0 || requestedLimit > 10) {
        originUrl.searchParams.set("limit", "10");
      }
    }
    originUrl.protocol = "http:";
    originUrl.hostname = ORIGIN_HOST;
    originUrl.port = ORIGIN_PORT;

    const init = {
      method: request.method,
      headers: forwardedHeaders(request),
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    const response = await fetch(originUrl, init);
    const headers = new Headers(response.headers);
    headers.set("x-cathedral-v2-beta-router", "cloudflare-worker");
    headers.set("x-cathedral-v2-beta-origin", "polaris-sandbox");
    const proxied = new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
    if (!cacheKey) return proxied;

    const ttl = await cacheableReceipt(proxied, receiptId);
    if (ttl === null) return clientReceiptResponse(proxied, "BYPASS");

    const storedHeaders = new Headers(proxied.headers);
    storedHeaders.set("cache-control", `public, max-age=${ttl}`);
    const stored = new Response(proxied.clone().body, {
      status: proxied.status,
      statusText: proxied.statusText,
      headers: storedHeaders,
    });
    try {
      await receiptCache.put(cacheKey, stored);
      return clientReceiptResponse(proxied, "MISS");
    } catch {
      return clientReceiptResponse(proxied, "ERROR");
    }
  },
};
