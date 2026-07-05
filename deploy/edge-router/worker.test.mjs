import assert from "node:assert/strict";
import {
  classifyEdgeHealth,
} from "./diagnose.mjs";
import {
  cachedFresh,
  cachePolicyForPath,
  canonicalPath,
  classifyRequest,
  handleRequest,
  normalizedCacheKeyUrl,
  originAllowsEdgeStore,
  originRequest,
  originTimeoutForRoute,
  responseIsVisibilityWarming,
  unsupportedCacheQueryParams,
} from "./worker.mjs";

assert.equal(canonicalPath("/api/cathedral/v1/leaderboard/top"), "/v1/leaderboard/top");
assert.equal(canonicalPath("/api/cathedral/v1/leaderboard/top/"), "/v1/leaderboard/top");
assert.equal(canonicalPath("/v1/leaderboard/top"), "/v1/leaderboard/top");

assert.deepEqual(
  classifyRequest("GET", "/v1/synthetic-boolean/active-challenges"),
  { role: "read", path: "/v1/synthetic-boolean/active-challenges" },
);
assert.deepEqual(
  classifyRequest("GET", "/api/cathedral/v1/synthetic-boolean/active-challenges"),
  { role: "read", path: "/v1/synthetic-boolean/active-challenges" },
);
assert.deepEqual(
  classifyRequest("GET", "/v1/synthetic-boolean/per-miner/cnf"),
  { role: "submit", path: "/v1/synthetic-boolean/per-miner/cnf" },
);
assert.deepEqual(
  classifyRequest("POST", "/v1/agents/submit"),
  { role: "submit", path: "/v1/agents/submit" },
);
assert.deepEqual(
  classifyRequest("GET", "/v1/unknown"),
  { role: "none", path: "/v1/unknown" },
);
assert.deepEqual(
  classifyRequest("POST", "/v1/audit-scanner/submit"),
  { role: "none", path: "/v1/audit-scanner/submit" },
);

assert.deepEqual(
  cachePolicyForPath("/v1/leaderboard/recent"),
  { freshTtl: 5, edgeTtl: 300, swr: true, params: ["limit", "since", "since_ran_at", "since_id"] },
);
assert.deepEqual(
  cachePolicyForPath("/v1/synthetic-boolean/active-challenges"),
  { freshTtl: 5, edgeTtl: 900, swr: true, params: [] },
);
assert.deepEqual(
  cachePolicyForPath("/v1/validator/weights/next"),
  { freshTtl: 15, edgeTtl: 300, swr: true, params: [] },
);
assert.equal(cachePolicyForPath("/v1/synthetic-boolean/per-miner/cnf"), null);

assert.equal(originTimeoutForRoute({}, "read", "/health/ready"), 9000);
assert.equal(originTimeoutForRoute({}, "read", "/.well-known/cathedral-jwks.json"), 9000);
assert.equal(originTimeoutForRoute({}, "read", "/v1/synthetic-boolean/active-challenges", true), 4500);
assert.equal(originTimeoutForRoute({}, "read", "/v1/synthetic-boolean/per-miner/status"), 4500);
assert.equal(originTimeoutForRoute({}, "submit", "/v1/agents/submit"), 10000);
assert.equal(
  originTimeoutForRoute(
    { READ_ORIGIN_TIMEOUT_MS: "10000" },
    "read",
    "/v1/synthetic-boolean/active-challenges",
    true,
  ),
  10000,
);
assert.equal(
  originTimeoutForRoute({ READ_HEALTH_ORIGIN_TIMEOUT_MS: "12000" }, "read", "/health/ready"),
  12000,
);

assert.equal(
  normalizedCacheKeyUrl(
    "https://api.cathedral.computer/v1/leaderboard/top?view=weights&window=24h",
    "/v1/leaderboard/top",
  ),
  "https://api.cathedral.computer/v1/leaderboard/top?window=24h&view=weights",
);
assert.equal(
  normalizedCacheKeyUrl(
    "https://api.cathedral.computer/api/cathedral/v1/synthetic-boolean/active-challenges",
    "/v1/synthetic-boolean/active-challenges",
  ),
  "https://api.cathedral.computer/v1/synthetic-boolean/active-challenges",
);
assert.deepEqual(
  unsupportedCacheQueryParams(
    "https://api.cathedral.computer/v1/leaderboard/top?x=random&view=weights",
    "/v1/leaderboard/top",
  ),
  ["x"],
);
assert.deepEqual(
  unsupportedCacheQueryParams(
    "https://api.cathedral.computer/v1/synthetic-boolean/active-challenges?_=cachebuster",
    "/v1/synthetic-boolean/active-challenges",
  ),
  [],
);
{
  const request = new Request(
    "https://api.cathedral.computer/v1/synthetic-boolean/active-challenges?_=cachebuster",
  );
  const normalized = normalizedCacheKeyUrl(request.url, "/v1/synthetic-boolean/active-challenges");
  const origin = originRequest(request, "https://read.cathedral.computer", {
    search: new URL(normalized).search,
  });
  assert.equal(origin.url, "https://read.cathedral.computer/v1/synthetic-boolean/active-challenges");
}
{
  const request = new Request("https://api.cathedral.computer/v1/validator/weights/next");
  const origin = originRequest(request, "https://2rdrh9ax.up.railway.app", {
    hostHeader: "api.cathedral.computer",
  });
  assert.equal(origin.url, "https://2rdrh9ax.up.railway.app/v1/validator/weights/next");
  assert.equal(origin.headers.get("host"), "api.cathedral.computer");
}
assert.deepEqual(
  unsupportedCacheQueryParams(
    "https://api.cathedral.computer/v1/leaderboard/recent?limit=20&since_id=abc",
    "/v1/leaderboard/recent",
  ),
  [],
);

const fresh = new Response("{}", { headers: { "X-Cathedral-Edge-Fresh-Until": "200" } });
const stale = new Response("{}", { headers: { "X-Cathedral-Edge-Fresh-Until": "100" } });
assert.equal(cachedFresh(fresh, 150), true);
assert.equal(cachedFresh(stale, 150), false);

assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Cache-Control": "public, max-age=10" },
})), true);
assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Cache-Control": "private, max-age=10" },
})), false);
assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Cache-Control": "no-store" },
})), false);
assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Vary": "Authorization" },
})), false);
assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Vary": "Accept" },
})), true);
assert.equal(originAllowsEdgeStore(new Response("{}", {
  headers: { "Vary": "Accept-Encoding" },
})), true);
assert.equal(await responseIsVisibilityWarming(new Response(
  JSON.stringify({ visibility_cache_status: "warming", items: [] }),
  { headers: { "Content-Type": "application/json" } },
)), true);
assert.equal(await responseIsVisibilityWarming(new Response(
  JSON.stringify({ data_status: "warming", current_signed_weight: null }),
  { headers: { "Content-Type": "application/json" } },
)), true);
assert.equal(await responseIsVisibilityWarming(new Response(
  JSON.stringify({ visibility: { sources: { payment: { status: "warming" } } } }),
  { headers: { "Content-Type": "application/json" } },
)), true);
assert.equal(await responseIsVisibilityWarming(new Response(
  JSON.stringify({ items: [{ id: "row" }] }),
  { headers: { "Content-Type": "application/json" } },
)), false);

const cfRateLimited = {
  status: 429,
  edge: "",
  server: "cloudflare",
  bodyKind: "cloudflare_rate_limited",
  body: "This website has been temporarily rate limited",
};
const cfWorkerPlanLimited = {
  status: 429,
  edge: "",
  server: "cloudflare",
  bodyKind: "cloudflare_worker_plan_limit",
  body: "Error 1027 owner has reached their plan limits",
};
const healthyRead = {
  status: 200,
  edge: "",
  server: "railway-hikari",
  bodyKind: "json",
  body: '{"service_role":"read"}',
};
const healthySubmit = {
  status: 200,
  edge: "",
  server: "railway-hikari",
  bodyKind: "json",
  body: '{"service_role":"submit"}',
};
const probeException = {
  status: 0,
  edge: "",
  server: "",
  bodyKind: "exception",
  body: "fetch failed",
};

for (const [name, probes, expected] of [
  [
    "api Cloudflare rate limited while split origins are healthy",
    { apiReady: cfRateLimited, readReady: healthyRead, submitReady: healthySubmit },
    "cloudflare_zone_rate_limited",
  ],
  [
    "api Cloudflare Worker plan limited while split origins are healthy",
    { apiReady: cfWorkerPlanLimited, readReady: healthyRead, submitReady: healthySubmit },
    "cloudflare_worker_plan_limit",
  ],
  [
    "api Worker readiness is healthy",
    {
      apiReady: {
        status: 200,
        edge: "BYPASS",
        server: "cloudflare",
        bodyKind: "json",
        body: '{"service_role":"read"}',
      },
      readReady: healthyRead,
      submitReady: healthySubmit,
    },
    "healthy",
  ],
  [
    "read probe exception is not origin unhealthy",
    { apiReady: cfRateLimited, readReady: probeException, submitReady: healthySubmit },
    "network_probe_error",
  ],
  [
    "api probe exception is not origin unhealthy",
    { apiReady: probeException, readReady: healthyRead, submitReady: healthySubmit },
    "network_probe_error",
  ],
  [
    "submit probe exception is not origin unhealthy",
    { apiReady: cfRateLimited, readReady: healthyRead, submitReady: probeException },
    "network_probe_error",
  ],
  [
    "read origin reports unhealthy",
    {
      apiReady: cfRateLimited,
      readReady: {
        status: 503,
        edge: "",
        server: "railway-hikari",
        bodyKind: "json",
        body: '{"service_role":"read","db":"error"}',
      },
      submitReady: healthySubmit,
    },
    "origin_unhealthy",
  ],
  [
    "Cloudflare response has no Worker edge header",
    {
      apiReady: {
        status: 429,
        edge: "",
        server: "cloudflare",
        bodyKind: "html_or_text",
        body: "<html>blocked</html>",
      },
      readReady: healthyRead,
      submitReady: healthySubmit,
    },
    "cloudflare_policy_or_route_gap",
  ],
  [
    "split origins healthy but api does not match a known state",
    {
      apiReady: {
        status: 503,
        edge: "BYPASS",
        server: "cloudflare",
        bodyKind: "json",
        body: '{"status":"error"}',
      },
      readReady: healthyRead,
      submitReady: healthySubmit,
    },
    "unknown_edge_state",
  ],
]) {
  assert.equal(classifyEdgeHealth(probes).status, expected, name);
}

const preflight = await handleRequest(new Request("https://api.cathedral.computer/v1/agents/submit", {
  method: "OPTIONS",
}));
assert.equal(preflight.status, 204);
assert.match(preflight.headers.get("Access-Control-Allow-Headers"), /x-cathedral-signature/);

const cacheBust = await handleRequest(new Request(
  "https://api.cathedral.computer/v1/leaderboard/top?x=random",
));
assert.equal(cacheBust.status, 400);
assert.match(await cacheBust.text(), /unsupported_cache_query_param/);

{
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      method: request.method,
      body: request.method === "POST" ? await request.clone().text() : "",
      shadowSource: request.headers.get("x-cathedral-shadow-source"),
    });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  };
  try {
    const waits = [];
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/agents/submit", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "card_id=synthetic_boolean_v1&challenge_id=pm-test&dimacs_solution=s+SATISFIABLE",
      }),
      {
        SUBMIT_ORIGIN: "https://submit-origin.example",
        SHADOW_V1_MIRROR_ENABLED: "true",
        SHADOW_V1_MIRROR_ORIGIN: "https://v2-shadow.example",
        SHADOW_V1_MIRROR_SAMPLE_PERCENT: "100",
        SHADOW_V1_MIRROR_SOURCE: "test-edge-mirror",
      },
      { waitUntil(promise) { waits.push(promise); } },
    );
    assert.equal(resp.status, 200);
    await Promise.all(waits);
    assert.equal(calls.length, 2);
    const originCall = calls.find((call) => call.url === "https://submit-origin.example/v1/agents/submit");
    const shadowCall = calls.find((call) => call.url === "https://v2-shadow.example/v2/shadow/v1/agents/submit");
    assert.ok(originCall);
    assert.ok(shadowCall);
    assert.equal(shadowCall.method, "POST");
    assert.equal(shadowCall.body, originCall.body);
    assert.equal(shadowCall.shadowSource, "test-edge-mirror");
  } finally {
    globalThis.fetch = originalFetch;
  }
}

{
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      method: request.method,
      contentType: request.headers.get("content-type"),
      body: request.method === "POST" ? await request.clone().text() : "",
      shadowSource: request.headers.get("x-cathedral-shadow-source"),
    });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  };
  try {
    const waits = [];
    const fullBody = "card_id=synthetic_boolean_v1&challenge_id=pm-test&dimacs_solution=s+SATISFIABLE%0Av+1+-2+0&submitted_at=2026-06-30T00%3A00%3A00.000Z";
    const resp = await handleRequest(
      new Request("https://submit.cathedral.computer/v1/agents/submit", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-Cathedral-Hotkey": "5TestHotkey",
          "X-Cathedral-Signature": "sig-test",
        },
        body: fullBody,
      }),
      {
        SUBMIT_ORIGIN: "https://submit-origin.example",
        SHADOW_V1_MIRROR_ENABLED: "true",
        SHADOW_V1_MIRROR_MODE: "meta",
        SHADOW_V1_MIRROR_ORIGIN: "https://v2-shadow.example",
        SHADOW_V1_MIRROR_SAMPLE_PERCENT: "100",
        SHADOW_V1_MIRROR_SOURCE: "test-edge-mirror-meta",
      },
      { waitUntil(promise) { waits.push(promise); } },
    );
    assert.equal(resp.status, 200);
    await Promise.all(waits);
    assert.equal(calls.length, 2);
    const originCall = calls.find((call) => call.url === "https://submit-origin.example/v1/agents/submit");
    const shadowCall = calls.find((call) => call.url === "https://v2-shadow.example/v2/shadow/v1/agents/submit/meta");
    assert.ok(originCall);
    assert.ok(shadowCall);
    assert.equal(originCall.body, fullBody);
    assert.equal(shadowCall.method, "POST");
    assert.equal(shadowCall.contentType, "application/json");
    assert.equal(shadowCall.shadowSource, "test-edge-mirror-meta");
    assert.doesNotMatch(shadowCall.body, /dimacs_solution=s\+SATISFIABLE/);
    const meta = JSON.parse(shadowCall.body);
    assert.equal(meta.metadata_only, undefined);
    assert.equal(meta.card_id, "synthetic_boolean_v1");
    assert.equal(meta.challenge_id, "pm-test");
    assert.equal(meta.miner_hotkey, "5TestHotkey");
    assert.equal(meta.signature_present, true);
    assert.ok(meta.dimacs_solution_bytes > 0);
    assert.ok(meta.original_body_bytes >= fullBody.length);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

console.log("edge-router worker tests passed");
