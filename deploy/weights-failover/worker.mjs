const STATIC_WEIGHTS_URL = "https://api.github.com/repos/wallscaler/cathedral-weight-feed/contents/v1/validator/weights/next?ref=main";
const RAW_STATIC_WEIGHTS_URL = "https://raw.githubusercontent.com/wallscaler/cathedral-weight-feed/main/v1/validator/weights/next";
const RAILWAY_FALLBACK_WEIGHTS_URL = "https://read.cathedral.computer/v1/validator/weights/next";
const LEGACY_PREFIX = "/api/cathedral";
const WEIGHTS_PATH = "/v1/validator/weights/next";
const FRESH_TTL_SECONDS = 120;
const EDGE_TTL_SECONDS = 900;

function canonicalPath(pathname) {
  let path = pathname;
  if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
  if (path.startsWith(`${LEGACY_PREFIX}/`)) return path.slice(LEGACY_PREFIX.length) || "/";
  return path;
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function cacheKey(request) {
  const url = new URL(request.url);
  url.pathname = WEIGHTS_PATH;
  url.search = "";
  return new Request(url.toString(), { method: "GET" });
}

function isFresh(response) {
  const until = Number(response.headers.get("x-cathedral-edge-fresh-until") || "0");
  return Number.isFinite(until) && until > nowSeconds();
}

function jsonResponse(payloadText, status, source, extraHeaders = {}) {
  const headers = new Headers(extraHeaders);
  headers.set("Content-Type", "application/json");
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", `public, max-age=2, s-maxage=${EDGE_TTL_SECONDS}`);
  headers.set("X-Cathedral-Vector-Source", source);
  headers.set("X-Cathedral-Edge-Fresh-Until", String(nowSeconds() + FRESH_TTL_SECONDS));
  return new Response(payloadText, { status, headers });
}

export function validatePayload(text) {
  const payload = JSON.parse(text);
  if (!payload || typeof payload !== "object") throw new Error("payload_not_object");
  if (payload.network !== "finney") throw new Error("bad_network");
  if (Number(payload.netuid) !== 39) throw new Error("bad_netuid");
  if (payload.key_id !== "cathedral-weight-policy") throw new Error("bad_key_id");
  if (!payload.signature || typeof payload.signature !== "string") throw new Error("missing_signature");
  if (!Array.isArray(payload.weights) || payload.weights.length === 0) throw new Error("bad_weights");
  const payable = payload.policy_metadata?.payable_hotkeys;
  if (!payable || payable.mode !== "filter" || payable.enforced !== true) {
    throw new Error("payable_filter_not_enforced");
  }
  if (payable.snapshot_fresh !== true || Number(payable.snapshot_hotkey_count) < 200) {
    throw new Error("bad_metagraph_snapshot");
  }
  if (Number(payable.final_miner_count) !== payload.weights.length) {
    throw new Error("filtered_count_mismatch");
  }
  if (!payload.expires_at || Date.parse(payload.expires_at) <= Date.now()) throw new Error("expired_vector");
  return payload;
}

async function fetchStatic() {
  const response = await fetch(STATIC_WEIGHTS_URL, {
    headers: {
      "Accept": "application/vnd.github.raw",
      "User-Agent": "cathedral-weights-failover",
    },
    cf: { cacheTtl: 60, cacheEverything: true },
  });
  if (response.status !== 200) throw new Error(`static_status_${response.status}`);
  const text = await response.text();
  validatePayload(text);
  return jsonResponse(text, 200, "static-github", {
    "X-Cathedral-Static-ETag": response.headers.get("etag") || "",
  });
}

async function fetchRawStatic() {
  const response = await fetch(RAW_STATIC_WEIGHTS_URL, {
    headers: { "User-Agent": "cathedral-weights-failover" },
    cf: { cacheTtl: 60, cacheEverything: true },
  });
  if (response.status !== 200) throw new Error(`raw_static_status_${response.status}`);
  const text = await response.text();
  validatePayload(text);
  return jsonResponse(text, 200, "static-github-raw", {
    "X-Cathedral-Raw-Static-ETag": response.headers.get("etag") || "",
  });
}

async function fetchRailwayFallback() {
  const response = await fetch(RAILWAY_FALLBACK_WEIGHTS_URL, {
    headers: { "User-Agent": "cathedral-weights-failover" },
    cf: { cacheTtl: 30, cacheEverything: true },
  });
  if (response.status !== 200) throw new Error(`railway_fallback_status_${response.status}`);
  const text = await response.text();
  validatePayload(text);
  return jsonResponse(text, 200, "railway-read-fallback");
}

async function refresh(cache, key) {
  let response;
  try {
    response = await fetchStatic();
  } catch (staticError) {
    try {
      response = await fetchRawStatic();
      response.headers.set("X-Cathedral-Static-Error", String(staticError && staticError.message || staticError));
    } catch (rawStaticError) {
      try {
        response = await fetchRailwayFallback();
        response.headers.set("X-Cathedral-Static-Error", String(staticError && staticError.message || staticError));
        response.headers.set("X-Cathedral-Raw-Static-Error", String(rawStaticError && rawStaticError.message || rawStaticError));
      } catch (railwayFallbackError) {
        throw new Error(`static_raw_and_railway_failed:${String(railwayFallbackError && railwayFallbackError.message || railwayFallbackError)}`);
      }
    }
  }
  await cache.put(key, response.clone());
  return response;
}

function withCacheHeader(response, value) {
  const out = new Response(response.body, response);
  out.headers.set("X-Cathedral-Edge-Cache", value);
  return out;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
          "Access-Control-Allow-Headers": "authorization, content-type",
          "Access-Control-Max-Age": "600",
        },
      });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return Response.json({ error: "method_not_allowed" }, { status: 405 });
    }
    const path = canonicalPath(new URL(request.url).pathname);
    if (path !== WEIGHTS_PATH) {
      return Response.json({ error: "not_found" }, { status: 404 });
    }

    const cache = caches.default;
    const key = cacheKey(request);
    const cached = await cache.match(key);
    if (cached && isFresh(cached)) {
      return withCacheHeader(cached, "HIT");
    }
    if (cached) {
      try {
        return withCacheHeader(await refresh(cache, key), "REFRESH");
      } catch (error) {
        const stale = withCacheHeader(cached, "STALE-FALLBACK");
        stale.headers.set("X-Cathedral-Refresh-Error", String(error && error.message || error));
        return stale;
      }
    }
    try {
      return withCacheHeader(await refresh(cache, key), "MISS");
    } catch (error) {
      return Response.json(
        { error: "weights_unavailable", detail: String(error && error.message || error) },
        { status: 504, headers: { "Cache-Control": "no-store" } },
      );
    }
  },
};
