import assert from "node:assert/strict";

const base = (process.env.CATHEDRAL_EDGE_BASE_URL ||
  "https://api.cathedral.computer").replace(/\/+$/, "");
const host = new URL(base).hostname;
const isWorkersDev = host.endsWith(".workers.dev");
const allowBypass = process.env.CATHEDRAL_EDGE_ALLOW_BYPASS === "1";
const expectWorker = isWorkersDev || !allowBypass;
const timeoutMs = Number(process.env.CATHEDRAL_EDGE_TIMEOUT_MS || 10000);

const EDGE_VALUES = new Set(["BYPASS", "HIT", "MISS", "REFRESH", "STALE", "STALE-REFRESH"]);
const CACHE_VALUES = new Set(["HIT", "MISS", "REFRESH", "STALE", "STALE-REFRESH"]);

const routed = [
  { path: "/health/ready", statuses: [200], expectedEdge: "BYPASS", bodyPattern: /"service_role":"read"/ },
  { path: "/.well-known/cathedral-jwks.json", statuses: [200], expectedEdge: "BYPASS" },
  { path: "/v1/synthetic-boolean/active-challenges", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/synthetic-boolean/active-challenges?_=route-map", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/synthetic-boolean/challenge-broadcast", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/synthetic-boolean/current-challenge", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/synthetic-boolean/active-cnf", statuses: [422, 429], expectedEdge: "BYPASS" },
  { path: "/v1/synthetic-boolean/per-miner/challenges", statuses: [422, 429], expectedEdge: "BYPASS" },
  { path: "/v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-test", statuses: [422, 429], expectedEdge: "BYPASS" },
  { path: "/v1/synthetic-boolean/per-miner/status", statuses: [422, 429], expectedEdge: "BYPASS" },
  { path: "/v1/synthetic-boolean/per-miner/summary", statuses: [200], expectedEdge: "cache" },
  { path: "/api/cathedral/v1/synthetic-boolean/active-challenges", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/leaderboard/recent?limit=2", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/leaderboard/top?window=24h", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/leaderboard/explain?miner_hotkey=5test", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/validator/weights/next", statuses: [200], expectedEdge: "cache" },
  { path: "/v1/agents/submit", method: "OPTIONS", statuses: [204], bypassStatuses: [405], expectedEdge: "none" },
  {
    path: "/v1/agents/submit",
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
    statuses: [422, 429],
    expectedEdge: "BYPASS",
  },
];

const passthrough = [
  { path: "/v1/synthetic-boolean/readiness-probe", statuses: [200] },
  { path: "/v1/synthetic-boolean/readiness-probe/cnf", statuses: [200] },
  { path: "/v1/arena/status", statuses: [200] },
  { path: "/v1/tee-gpu/offers", statuses: [422] },
  { path: "/v1/audit-scanner/leaderboard", statuses: [404] },
];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function request(check) {
  const started = Date.now();
  const res = await fetch(`${base}${check.path}`, {
    method: check.method || "GET",
    headers: check.headers || {},
    body: check.body,
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await res.text();
  return {
    ...check,
    status: res.status,
    ms: Date.now() - started,
    edge: res.headers.get("x-cathedral-edge-cache") || "",
    server: res.headers.get("server") || "",
    body,
  };
}

function print(result, group) {
  const method = result.method || "GET";
  const edge = result.edge || "-";
  console.log(
    `${group.padEnd(11)} ${method.padEnd(7)} ${String(result.status).padStart(3)} ` +
    `${String(result.ms).padStart(5)}ms ${edge.padEnd(13)} ${result.path}`,
  );
}

function assertStatus(result) {
  const statuses = (!expectWorker && result.bypassStatuses) ? result.bypassStatuses : result.statuses;
  assert.ok(
    statuses.includes(result.status),
    `${result.path} returned ${result.status}, expected ${statuses.join("/")}`,
  );
}

function assertRouted(result) {
  assertStatus(result);
  if (!expectWorker) {
    assert.equal(result.edge, "", `${result.path} bypass mode should not show edge header`);
    return;
  }
  assert.match(result.server, /cloudflare/i, `${result.path} did not pass through Cloudflare`);

  if (result.expectedEdge === "none") {
    assert.equal(result.edge, "");
  } else if (result.expectedEdge === "cache") {
    assert.ok(CACHE_VALUES.has(result.edge), `${result.path} missing cache edge header`);
  } else {
    assert.equal(result.edge, result.expectedEdge, `${result.path} missing expected edge header`);
  }
  assert.ok(
    result.edge === "" || EDGE_VALUES.has(result.edge),
    `${result.path} returned unexpected edge header ${result.edge}`,
  );
  if (result.bodyPattern instanceof RegExp) {
    assert.match(result.body, result.bodyPattern);
  }
}

function assertPassthrough(result) {
  assertStatus(result);
  if (!expectWorker) {
    assert.equal(result.edge, "", `${result.path} bypass mode should not show edge header`);
    return;
  }
  assert.match(result.server, /cloudflare/i, `${result.path} did not pass through Cloudflare`);
  if (isWorkersDev) return;
  assert.equal(result.edge, "", `${result.path} should pass through without edge header`);
  assert.doesNotMatch(result.body, /route_not_served_by_cathedral_edge/);
}

async function run() {
  console.log(`route-map base=${base} expectWorker=${expectWorker} allowBypass=${allowBypass}`);

  const results = [];
  for (const check of routed) {
    let result = await request(check);
    print(result, "routed");
    if (expectWorker && check.expectedEdge === "cache" && !CACHE_VALUES.has(result.edge)) {
      assertStatus(result);
      await sleep(250);
      result = await request(check);
      print(result, "routed");
    }
    assertRouted(result);
    results.push(result);
  }

  if (!isWorkersDev) {
    for (const check of passthrough) {
      const result = await request(check);
      print(result, "passthrough");
      assertPassthrough(result);
      results.push(result);
    }
  }

  if (expectWorker) {
    assert.ok(
      results.some((result) => result.edge && CACHE_VALUES.has(result.edge)),
      "no cached routed endpoint returned an edge cache header",
    );
  }

  if (allowBypass) {
    console.log(`origin-direct route map passed for ${base}; Worker was intentionally not verified`);
  } else {
    console.log(`edge-router route map passed for ${base}`);
  }
}

await run();
