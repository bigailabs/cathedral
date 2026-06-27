// Cathedral board-failover edge worker.
//
// Purpose: protect the cheap miner *board* reads (Tier-1) the same way the
// validator weight feed was protected, by serving them from a healthy
// publisher origin that is independent of the slow leaderboard read service.
//
// The cheap board reads (active-challenges / challenge-broadcast /
// current-challenge / per-miner status+summary) are cheap snapshot queries.
// They were going 504 because they shared the read service with the expensive
// `/v1/leaderboard/recent` cursor scan (and `/v1/leaderboard/top`). One slow
// hog saturating that service starved the cheap board reads.
//
// This worker splits reads by *origin*, not just by cache, AND it honours the
// read/submit role split that the main edge router (../worker.mjs) enforces:
//
//   - BOARD (Tier-1 read) GETs   -> BOARD_ORIGIN        (healthy read/publisher origin)
//   - /v1/leaderboard/{recent,top} -> LEADERBOARD_ORIGIN (existing read service, isolated)
//   - SUBMIT-role paths          -> SUBMIT_ORIGIN       (submit origin; NOT a read origin)
//
// CRITICAL: the submit-role paths (active-cnf, per-miner/challenges,
// per-miner/cnf, /v1/challenges/*, POST /v1/agents/submit) are served in prod by
// the SUBMIT origin. A read-role origin 404s them. They must go to SUBMIT_ORIGIN
// (default submit.cathedral.computer) -- never the read/board origin -- or board
// failover would break per-miner CNF delivery and submit.
//
// The slow leaderboard stays isolated on its own origin and can saturate only
// itself; it can no longer take the board down with it. The board origin is the
// publisher service that already serves these snapshots from in-memory caches.
//
// This worker is intentionally separate from ../worker.mjs. It is a focused,
// reviewable failover that can be attached to ONLY the board hot paths during an
// incident and detached on rollback without touching the main edge router.
//
// It does not cache (the upstream ../worker.mjs already owns edge caching for
// these paths). Its single job is origin isolation: keep the cheap board reads
// off the jammed leaderboard service while keeping submit-role paths on submit.

const DEFAULT_BOARD_ORIGIN = "https://read.cathedral.computer";
const DEFAULT_LEADERBOARD_ORIGIN = "https://read.cathedral.computer";
const DEFAULT_SUBMIT_ORIGIN = "https://submit.cathedral.computer";

const LEGACY_PREFIX = "/api/cathedral";

// --- OPT-IN: durable last-known-good (LKG) for the cheap board reads ---------
//
// Mirrors the weights-failover pattern (deploy/edge-router/weights-failover/):
// when a BOARD_LKG KV namespace is bound, healthy board-tier (Tier-1) responses
// are mirrored to KV and served as last-known-good if the board origin is
// unhealthy (5xx / timeout / network error). The board is a materialized
// snapshot (publisher-side scaffold/publisher/materialized_snapshot.py), so the
// last-good body is a safe thing to re-serve briefly during an origin blip.
//
// DEFAULT-OFF by construction: with no BOARD_LKG binding (the committed
// wrangler.toml leaves it commented), boardLkgEnabled() is false and the worker
// behaves byte-identically to before — origin failure still returns 504. Only
// the board tier participates; leaderboard/submit tiers are never mirrored
// (submit must never serve a stale body, and the leaderboard is the slow hog we
// deliberately do not cache here).
const BOARD_LKG_KEY_PREFIX = "lkg:board:";

function boardLkgEnabled(env) {
  return !!(
    env &&
    env.BOARD_LKG &&
    typeof env.BOARD_LKG.getWithMetadata === "function" &&
    typeof env.BOARD_LKG.put === "function"
  );
}

// Per-path KV key — the board tier serves several distinct paths
// (active-challenges, challenge-broadcast, current-challenge, per-miner/*), each
// with its own body, so each gets its own last-known-good entry.
export function boardLkgKey(path) {
  return BOARD_LKG_KEY_PREFIX + path;
}

// Serve the last-known-good board body from KV, or null if KV was never
// populated for this path (caller then falls through to the existing 504).
export async function serveBoardStale(env, path, reason) {
  if (!boardLkgEnabled(env)) return null;
  let lkg;
  try {
    lkg = await env.BOARD_LKG.getWithMetadata(boardLkgKey(path));
  } catch (_e) {
    return null;
  }
  if (!lkg || !lkg.value) return null;
  const storedAt = (lkg.metadata && lkg.metadata.stored_at) || "";
  const contentType =
    (lkg.metadata && lkg.metadata.content_type) || "application/json";
  return new Response(lkg.value, {
    status: 200,
    headers: {
      "content-type": contentType,
      "access-control-allow-origin": "*",
      // Never let a stale fallback be re-cached at the edge as if fresh.
      "cache-control": "no-store",
      "x-cathedral-board-source": "stale_fallback",
      "x-cathedral-fallback-reason": reason,
      "x-cathedral-board-stored-at": storedAt,
      "X-Cathedral-Board-Tier": "board",
    },
  });
}

// Tier-1: cheap miner board reads served by the READ role. These must stay
// healthy. They move to the publisher/board origin so the slow leaderboard
// cannot starve them. These are exactly the READ-role board GETs from the main
// router's READ_GET_PATHS (minus leaderboard, which is Tier-2 below).
const BOARD_GET_PATHS = new Set([
  "/v1/synthetic-boolean/active-challenges",
  "/v1/synthetic-boolean/challenge-broadcast",
  "/v1/synthetic-boolean/current-challenge",
  "/v1/synthetic-boolean/per-miner/status",
  "/v1/synthetic-boolean/per-miner/summary",
]);

// Tier-2: the slow leaderboard hog. Deliberately LEFT on the existing read
// service so its load stays isolated from the board. Do not move these to the
// board origin or the isolation is defeated.
const LEADERBOARD_GET_PATHS = new Set([
  "/v1/leaderboard/recent",
  "/v1/leaderboard/top",
]);

// Submit-role GETs. In prod these are served by the SUBMIT origin; a read-role
// origin 404s them. They MUST go to SUBMIT_ORIGIN. Kept in sync with the main
// router's SUBMIT_GET_PATHS so the role split is identical at both layers.
const SUBMIT_GET_PATHS = new Set([
  "/v1/synthetic-boolean/active-cnf",
  "/v1/synthetic-boolean/per-miner/challenges",
  "/v1/synthetic-boolean/per-miner/cnf",
]);

// Submit-role GET prefixes (private challenge fetch), kept in sync with the main
// router's SUBMIT_GET_PREFIXES.
const SUBMIT_GET_PREFIXES = [
  "/v1/challenges/",
];

// Submit-role POSTs, kept in sync with the main router's SUBMIT_POST_PATHS.
const SUBMIT_POST_PATHS = new Set([
  "/v1/agents/submit",
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

// Returns which origin tier a request belongs to.
//   "board"       -> BOARD_ORIGIN (protected Tier-1 read)
//   "leaderboard" -> LEADERBOARD_ORIGIN (isolated Tier-2 read)
//   "submit"      -> SUBMIT_ORIGIN (submit role; never a read origin)
//   "none"        -> not served by this worker (default-deny)
export function classifyBoardRequest(method, pathname) {
  const path = canonicalPath(pathname);
  if (method === "GET" || method === "HEAD") {
    if (BOARD_GET_PATHS.has(path)) {
      return { tier: "board", path };
    }
    if (LEADERBOARD_GET_PATHS.has(path)) {
      return { tier: "leaderboard", path };
    }
    if (SUBMIT_GET_PATHS.has(path) || hasPrefix(path, SUBMIT_GET_PREFIXES)) {
      return { tier: "submit", path };
    }
  }
  if (method === "POST" && SUBMIT_POST_PATHS.has(path)) {
    return { tier: "submit", path };
  }
  return { tier: "none", path };
}

function cloneHeaders(headers) {
  const out = new Headers(headers);
  out.delete("host");
  return out;
}

export function originRequest(request, originBase, options = {}) {
  const incoming = new URL(request.url);
  const origin = new URL(originBase);
  origin.pathname = canonicalPath(incoming.pathname);
  origin.search = incoming.search;
  const headers = cloneHeaders(request.headers);
  if (options.hostHeader) {
    headers.set("Host", options.hostHeader);
  }
  return new Request(origin.toString(), {
    method: request.method,
    headers,
    body: request.body,
    redirect: "manual",
  });
}

// Origin selection for a classified tier. Submit-role tiers go to SUBMIT_ORIGIN
// (default submit.cathedral.computer); read tiers go to their read origin.
export function originForTier(env, tier) {
  if (tier === "board") {
    return {
      base: envUrl(env, "BOARD_ORIGIN", DEFAULT_BOARD_ORIGIN),
      host: envUrl(env, "BOARD_ORIGIN_HOST", ""),
      resolveOverride: envUrl(env, "BOARD_ORIGIN_RESOLVE_OVERRIDE", ""),
    };
  }
  if (tier === "submit") {
    return {
      base: envUrl(env, "SUBMIT_ORIGIN", DEFAULT_SUBMIT_ORIGIN),
      host: envUrl(env, "SUBMIT_ORIGIN_HOST", ""),
      resolveOverride: envUrl(env, "SUBMIT_ORIGIN_RESOLVE_OVERRIDE", ""),
    };
  }
  return {
    base: envUrl(env, "LEADERBOARD_ORIGIN", DEFAULT_LEADERBOARD_ORIGIN),
    host: envUrl(env, "LEADERBOARD_ORIGIN_HOST", ""),
    resolveOverride: envUrl(env, "LEADERBOARD_ORIGIN_RESOLVE_OVERRIDE", ""),
  };
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

// Board reads get a tight timeout: they are cheap snapshots and should answer
// fast. The leaderboard keeps a looser timeout because it is the slow path.
// Submit gets the submit timeout (verification can be slower than a snapshot).
export function originTimeoutForTier(env, tier) {
  if (tier === "board") {
    return originTimeoutMs(env, "BOARD_ORIGIN_TIMEOUT_MS", 4500);
  }
  if (tier === "submit") {
    return originTimeoutMs(env, "SUBMIT_ORIGIN_TIMEOUT_MS", 10000);
  }
  return originTimeoutMs(env, "LEADERBOARD_ORIGIN_TIMEOUT_MS", 9000);
}

function responseWithEdgeHeaders(response, headers) {
  const out = new Response(response.body, response);
  for (const [key, value] of Object.entries(headers)) {
    out.headers.set(key, value);
  }
  if (!out.headers.has("Access-Control-Allow-Origin")) {
    out.headers.set("Access-Control-Allow-Origin", "*");
  }
  return out;
}

// CORS preflight for the board/submit hot paths. Mirrors the main edge router's
// preflightResponse so attaching board-failover does not break browser submit.
export function preflightResponse() {
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

export async function handleRequest(request, env = {}, ctx = { waitUntil() {} }) {
  if (request.method === "OPTIONS") {
    return preflightResponse();
  }

  const url = new URL(request.url);
  const route = classifyBoardRequest(request.method, url.pathname);
  if (route.tier === "none") {
    // DEFAULT-DENY: any path this worker does not explicitly classify gets a 404.
    // This is SAFE ONLY because this worker is attached to the exact hot paths in
    // wrangler.toml (board reads, leaderboard, submit-role).
    //
    // !!! NEVER ATTACH THIS WORKER AS A CATCH-ALL (e.g. `*` /
    // `api.cathedral.computer/*`). A catch-all would make this default-deny 404
    // every unrelated API path (health, jwks, validator weight feed, admin, ...)
    // and take the whole surface down. Attach ONLY the per-path routes listed in
    // wrangler.toml. See README.md "NEVER attach as catch-all".
    return Response.json(
      { error: "route_not_served_by_board_failover", path: route.path },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }

  const origin = originForTier(env, route.tier);
  const originReq = originRequest(request, origin.base, { hostHeader: origin.host });
  const isBoard = route.tier === "board";
  const waitUntil =
    ctx && typeof ctx.waitUntil === "function" ? ctx.waitUntil.bind(ctx) : () => {};

  let resp;
  try {
    resp = await fetchWithTimeout(originReq, originTimeoutForTier(env, route.tier), {
      resolveOverride: origin.resolveOverride,
    });
  } catch (error) {
    // Board tier only: serve the durable last-known-good if KV has one.
    if (isBoard) {
      const stale = await serveBoardStale(env, route.path, "origin_error");
      if (stale) return stale;
    }
    return Response.json(
      {
        error: `${route.tier}_origin_unavailable`,
        detail: String((error && error.message) || error),
      },
      { status: 504, headers: { "Cache-Control": "no-store" } },
    );
  }

  // Board tier: on an origin 5xx, prefer the last-known-good snapshot over
  // propagating the error (degrade to stale, not to error). On a healthy
  // response with a non-trivial body, mirror it to KV as the new LKG.
  if (isBoard && boardLkgEnabled(env)) {
    if (resp.status >= 500) {
      const stale = await serveBoardStale(env, route.path, "origin_status_" + resp.status);
      if (stale) return stale;
    } else if (resp.ok) {
      const mirror = resp.clone();
      waitUntil(
        mirror.text().then((body) => {
          if (body && body.length > 2) {
            return env.BOARD_LKG.put(boardLkgKey(route.path), body, {
              metadata: {
                stored_at: new Date().toISOString(),
                content_type: resp.headers.get("content-type") || "application/json",
              },
            });
          }
        }).catch(() => {}),
      );
    }
  }

  return responseWithEdgeHeaders(resp, {
    "X-Cathedral-Board-Tier": route.tier,
    "X-Cathedral-Edge-Cache": "BYPASS",
    "Cache-Control": resp.headers.get("Cache-Control") || "no-store",
  });
}

export default {
  fetch: handleRequest,
};
