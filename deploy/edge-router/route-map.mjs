import assert from "node:assert/strict";

const base = (process.env.CATHEDRAL_EDGE_BASE_URL ||
  "https://api.cathedral.computer").replace(/\/+$/, "");
const host = new URL(base).hostname;
const isWorkersDev = host.endsWith(".workers.dev");
const allowBypass = process.env.CATHEDRAL_EDGE_ALLOW_BYPASS === "1";
const expectWorker = isWorkersDev || !allowBypass;
const timeoutMs = Number(process.env.CATHEDRAL_EDGE_TIMEOUT_MS || 10000);
const retryAttempts = Math.max(1, Number(process.env.CATHEDRAL_ROUTE_MAP_ATTEMPTS || 5));
const retryDelayMs = Math.max(0, Number(process.env.CATHEDRAL_ROUTE_MAP_RETRY_DELAY_MS || 1000));

const EDGE_VALUES = new Set(["BYPASS", "HIT", "MISS", "REFRESH", "STALE", "STALE-REFRESH"]);
const CACHE_VALUES = new Set(["HIT", "MISS", "REFRESH", "STALE", "STALE-REFRESH"]);
const TRANSIENT_STATUSES = new Set([500, 502, 503, 504]);

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
  { path: "/v1/verifiable-sat/coinbase/status", statuses: [200], expectedEdge: "BYPASS" },
  { path: "/v1/verifiable-sat/coinbase/challenge", statuses: [422, 429], expectedEdge: "BYPASS" },
  { path: "/api/cathedral/v1/verifiable-sat/coinbase/challenge", statuses: [422, 429], expectedEdge: "BYPASS" },
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
  {
    path: "/v1/verifiable-sat/coinbase/verify",
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
    statuses: [422, 429],
    expectedEdge: "BYPASS",
  },
  {
    path: "/v1/verifiable-sat/coinbase/submit",
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
  const statuses = expectedStatuses(result);
  assert.ok(
    statuses.includes(result.status),
    `${result.path} returned ${result.status}, expected ${statuses.join("/")}`,
  );
}

function expectedStatuses(check) {
  return (!expectWorker && check.bypassStatuses) ? check.bypassStatuses : check.statuses;
}

function shouldRetry(check, result) {
  return check.expectedEdge === "cache" && TRANSIENT_STATUSES.has(result.status);
}

async function requestEventually(check) {
  let lastError;
  for (let attempt = 1; attempt <= retryAttempts; attempt += 1) {
    try {
      const result = await request(check);
      if (
        expectedStatuses(check).includes(result.status) ||
        !shouldRetry(check, result) ||
        attempt === retryAttempts
      ) {
        return result;
      }
      console.log(
        `retry ${attempt}/${retryAttempts} ${result.status} ${check.path} after ${retryDelayMs}ms`,
      );
    } catch (err) {
      lastError = err;
      if (attempt === retryAttempts) throw err;
      console.log(
        `retry ${attempt}/${retryAttempts} ${check.path} error=${err.message} after ${retryDelayMs}ms`,
      );
    }
    await sleep(retryDelayMs);
  }
  throw lastError;
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
    let result = await requestEventually(check);
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
