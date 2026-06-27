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

function freshHeaders(source, reason, storedAt) {
  const h = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "x-cathedral-vector-source": source,
  };
  if (source === "origin") {
    // Short fresh-vector edge cache for healthy responses.
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

// Serve the last-known-good vector from KV, or 503 if KV was never populated.
export async function serveStale(env, reason) {
  const lkg = await env.WEIGHTS_LKG.getWithMetadata(KEY);
  if (lkg && lkg.value) {
    const storedAt = (lkg.metadata && lkg.metadata.stored_at) || "";
    return json(lkg.value, 200, "stale_fallback", reason, storedAt);
  }
  return new Response(
    JSON.stringify({ error: "no vector available", reason }),
    { status: 503, headers: { "content-type": "application/json", "cache-control": "no-store" } },
  );
}

export async function handleRequest(request, env, ctx) {
  const waitUntil = ctx && ctx.waitUntil ? ctx.waitUntil.bind(ctx) : () => {};
  const url = new URL(request.url);
  const path = canonicalPath(url.pathname);
  const originUrl = env.PUBLISHER_ORIGIN.replace(/\/+$/, "") + path + url.search;

  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), originTimeoutMs(env));
    let resp;
    try {
      resp = await fetch(originUrl, {
        method: "GET",
        headers: { accept: "application/json" },
        signal: ctl.signal,
        // Let our own response Cache-Control drive the edge cache; do not let
        // the subrequest serve a stale Cloudflare-cached copy back to us.
        cf: { cacheTtl: 0 },
      });
    } finally {
      clearTimeout(timer);
    }
    if (resp.ok) {
      const body = await resp.text();
      if (body && body.length > 2) {
        waitUntil(
          env.WEIGHTS_LKG.put(KEY, body, { metadata: { stored_at: new Date().toISOString() } }),
        );
      }
      return json(body, 200, "origin", "");
    }
    return await serveStale(env, "origin_status_" + resp.status);
  } catch (e) {
    return await serveStale(env, "origin_error");
  }
}

export default {
  async fetch(request, env, ctx) {
    return handleRequest(request, env, ctx);
  },
};
