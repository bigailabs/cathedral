// cathedral-weights-failover (Cloudflare Worker)
//
// CAPTURE / RECONSTRUCTION — NOT byte-verified against the live deployed worker.
// This file is a version-controlled reconstruction of the durable last-known-good
// (LKG) failover for the validator weight feed (RELIABILITY_UPGRADE_PLAN P0 #2).
// Until the live worker's source hash is recorded and matched (see README
// "Recording the live script hash"), treat this as the intended behavior, not a
// proven mirror of what is running.
//
// Behavior:
//   - Proxies GET /v1/validator/weights/next to the publisher origin.
//   - On origin 2xx with a non-trivial signed body, mirrors the body to KV
//     (WEIGHTS_LKG) as the last-known-good vector and serves it with short
//     fresh-vector edge caching.
//   - On origin 5xx / network error / timeout, serves the LKG body from KV with
//     an explicit x-cathedral-vector-source: stale_fallback marker.
//   - If the origin is down AND KV is empty, returns 503.
//
// The served body is the original signed bytes, verbatim. Validators detect
// staleness from the BODY (the signed vector carries generated_at / expires_at);
// the worker never re-signs or mutates the vector.

const LEGACY_PREFIX = "/api/cathedral";
const KEY = "lkg:weights_next";
const ORIGIN_TIMEOUT_MS = 8000;

// Short fresh-vector edge cache. The signed vector refreshes ~every 60s at the
// origin, so a brief shared-cache window keeps the bulk of healthy validator
// reads off the origin while staying well inside the 5-min freshness budget.
// Cloudflare's edge honors these response Cache-Control directives.
const EDGE_FRESH_TTL = 15; // s-maxage: shared (edge) cache freshness
const EDGE_SWR_TTL = 1200; // stale-while-revalidate window during refresh

// Strip the legacy /api/cathedral prefix, returning the canonical path.
export function canonicalPath(pathname) {
  let path = pathname;
  if (path.startsWith(LEGACY_PREFIX)) path = path.slice(LEGACY_PREFIX.length) || "/";
  return path;
}

export function originTimeoutMs(env) {
  const raw = env && env.WEIGHTS_ORIGIN_TIMEOUT_MS;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : ORIGIN_TIMEOUT_MS;
}

// Current time in ms. Overridable via env for deterministic tests; defaults to
// wall clock in production.
export function nowMs(env) {
  const raw = env && env.WEIGHTS_NOW_MS;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : Date.now();
}

// Fail-closed freshness predicate. A body is fresh ONLY if it parses as JSON, is
// an object, carries a string `expires_at` that is a valid timestamp, and that
// timestamp is strictly in the future. Every other case — empty/trivial body,
// parse error, missing/invalid `expires_at`, non-future — is NOT fresh, so it is
// never mirrored to LKG and never served as a 200. This is what stops an
// expired-but-200 origin body from poisoning KV or reaching validators.
export function vectorIsFresh(body, now) {
  if (!body || body.length <= 2) return false;
  let obj;
  try {
    obj = JSON.parse(body);
  } catch (_e) {
    return false;
  }
  if (!obj || typeof obj !== "object") return false;
  const exp = obj.expires_at;
  if (!exp || typeof exp !== "string") return false;
  const t = Date.parse(exp);
  if (!Number.isFinite(t)) return false;
  return t > now;
}

function freshHeaders(source, reason, storedAt) {
  const h = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "x-cathedral-vector-source": source,
  };
  if (source === "origin" || source === "artifact") {
    // Short fresh-vector edge cache for healthy responses. Both the shared
    // artifact and a first-fill origin response are safe to briefly edge-cache.
    h["cache-control"] =
      `public, max-age=2, s-maxage=${EDGE_FRESH_TTL}, stale-while-revalidate=${EDGE_SWR_TTL}`;
  } else {
    // Never let a stale fallback be re-cached at the edge as if fresh.
    h["cache-control"] = "no-store";
  }
  if (reason) h["x-cathedral-fallback-reason"] = reason;
  if (storedAt) h["x-cathedral-vector-stored-at"] = storedAt;
  return h;
}

function json(body, status, source, reason, storedAt) {
  return new Response(body, { status, headers: freshHeaders(source, reason, storedAt) });
}

// Serve the last-known-good vector from KV — but ONLY if it is still fresh.
// A non-fresh (expired/malformed) LKG body is never served as a 200; instead we
// fail closed with 503 weights_expired so a validator gets a clear retry signal
// rather than expired signed consensus dressed as success.
export async function serveStale(env, reason) {
  const lkg = await env.WEIGHTS_LKG.getWithMetadata(KEY);
  const hasBody = !!(lkg && lkg.value);
  if (hasBody && vectorIsFresh(lkg.value, nowMs(env))) {
    const storedAt = (lkg.metadata && lkg.metadata.stored_at) || "";
    return json(lkg.value, 200, "stale_fallback", reason, storedAt);
  }
  // Fail closed: either KV is empty, or it holds a stale/poisoned body.
  return new Response(
    JSON.stringify({
      error: hasBody ? "weights_expired" : "no vector available",
      reason,
    }),
    {
      status: 503,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
        "retry-after": "2",
        "x-cathedral-fallback-reason": reason,
      },
    },
  );
}

// generated_at of a vector in ms. Prefers the cheap metadata copy, falls back to
// parsing the body. Returns -Infinity when unknown so it always loses a monotonic
// comparison.
export function parseGenMs(body, meta) {
  if (meta && typeof meta.generated_at === "string") {
    const t = Date.parse(meta.generated_at);
    if (Number.isFinite(t)) return t;
  }
  try {
    const o = JSON.parse(body);
    const t = Date.parse(o && o.generated_at);
    if (Number.isFinite(t)) return t;
  } catch (_e) {
    /* fall through */
  }
  return -Infinity;
}

function generatedAtOf(body) {
  try {
    return JSON.parse(body).generated_at;
  } catch (_e) {
    return undefined;
  }
}

// Fetch the origin weight vector. Returns { ok, status, body }. Throws on network
// error / timeout (caller treats that as origin_error).
async function fetchOriginBody(originUrl, env) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), originTimeoutMs(env));
  try {
    const resp = await fetch(originUrl, {
      method: "GET",
      headers: { accept: "application/json" },
      signal: ctl.signal,
      cf: { cacheTtl: 0 },
    });
    const body = resp.ok ? await resp.text() : "";
    return { ok: resp.ok, status: resp.status, body };
  } finally {
    clearTimeout(timer);
  }
}

// Monotonic write: store `body` as the shared artifact ONLY if it is strictly
// newer (by generated_at) than the currently-stored vector — UNLESS the current
// one is not fresh, in which case any fresh body replaces it. This is what stops
// validators flapping between different fresh generations: the artifact only ever
// moves forward, never regresses to an older fresh vector.
export async function putArtifact(env, body, current, now) {
  const newGen = parseGenMs(body, null);
  const curFresh = !!(current && current.value && vectorIsFresh(current.value, now));
  const curGen = curFresh ? parseGenMs(current.value, current.metadata) : -Infinity;
  if (newGen <= curGen) return false;
  await env.WEIGHTS_LKG.put(KEY, body, {
    metadata: { stored_at: new Date(now).toISOString(), generated_at: generatedAtOf(body) },
  });
  return true;
}

// Background: pull origin and advance the artifact if origin has a strictly-newer
// fresh vector. Best-effort; never throws into the request path.
export async function maybeAdvance(originUrl, env) {
  try {
    const o = await fetchOriginBody(originUrl, env);
    if (!o.ok || !vectorIsFresh(o.body, nowMs(env))) return;
    const current = await env.WEIGHTS_LKG.getWithMetadata(KEY);
    await putArtifact(env, o.body, current, nowMs(env));
  } catch (_e) {
    /* best-effort */
  }
}

export async function handleRequest(request, env, ctx) {
  const waitUntil = ctx && ctx.waitUntil ? ctx.waitUntil.bind(ctx) : () => {};
  const url = new URL(request.url);
  const path = canonicalPath(url.pathname);
  const originUrl = env.PUBLISHER_ORIGIN.replace(/\/+$/, "") + path + url.search;
  const now = nowMs(env);

  // Artifact-first: the shared KV vector is the source of truth for reads. If it
  // is fresh, every validator gets the SAME body, regardless of which origin
  // replica is healthy. We refresh it from origin in the background (monotonic).
  const lkg = await env.WEIGHTS_LKG.getWithMetadata(KEY);
  if (lkg && lkg.value && vectorIsFresh(lkg.value, now)) {
    const storedAt = (lkg.metadata && lkg.metadata.stored_at) || "";
    waitUntil(maybeAdvance(originUrl, env));
    return json(lkg.value, 200, "artifact", "", storedAt);
  }

  // No fresh artifact (empty or expired): we must fetch origin synchronously to
  // get something to serve, and seed/replace the artifact (monotonic).
  try {
    const o = await fetchOriginBody(originUrl, env);
    if (o.ok && vectorIsFresh(o.body, nowMs(env))) {
      await putArtifact(env, o.body, lkg, nowMs(env));
      return json(o.body, 200, "origin", "");
    }
    return await serveStale(env, o.ok ? "origin_not_fresh" : "origin_status_" + o.status);
  } catch (_e) {
    return await serveStale(env, "origin_error");
  }
}

export default {
  async fetch(request, env, ctx) {
    return handleRequest(request, env, ctx);
  },
};
