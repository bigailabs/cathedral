import assert from "node:assert/strict";
import {
  cachedFresh,
  cachePolicyForPath,
  canonicalPath,
  classifyRequest,
  handleRequest,
  normalizedCacheKeyUrl,
  originAllowsEdgeStore,
  originRequest,
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
  { freshTtl: 2, edgeTtl: 20, swr: true, params: ["limit", "since", "since_ran_at", "since_id"] },
);
assert.equal(cachePolicyForPath("/v1/synthetic-boolean/per-miner/cnf"), null);

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

console.log("edge-router worker tests passed");
