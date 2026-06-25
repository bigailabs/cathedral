import assert from "node:assert/strict";

const base = (process.env.CATHEDRAL_EDGE_BASE_URL ||
  "https://api.cathedral.computer").replace(/\/+$/, "");
const host = new URL(base).hostname;
const isWorkersDev = host.endsWith(".workers.dev");
const allowBypass = process.env.CATHEDRAL_EDGE_ALLOW_BYPASS === "1";
const expectWorker = isWorkersDev || !allowBypass;
const iterations = Number(process.env.CATHEDRAL_EDGE_SOAK_ITERATIONS || 12);
const intervalMs = Number(process.env.CATHEDRAL_EDGE_SOAK_INTERVAL_MS || 5000);
const timeoutMs = Number(process.env.CATHEDRAL_EDGE_TIMEOUT_MS || 10000);
const includeTop = process.env.CATHEDRAL_EDGE_INCLUDE_TOP === "1";

const EDGE_VALUES = new Set(["BYPASS", "HIT", "MISS", "REFRESH", "STALE", "STALE-REFRESH"]);

const checks = [
  { name: "ready", path: "/health/ready", statuses: [200], edge: "BYPASS" },
  { name: "active", path: "/v1/synthetic-boolean/active-challenges", statuses: [200], edge: "cache" },
  { name: "recent", path: "/v1/leaderboard/recent?limit=2", statuses: [200], edge: "cache" },
  { name: "weights", path: "/v1/validator/weights/next", statuses: [200], edge: "cache" },
  {
    name: "submit",
    path: "/v1/agents/submit",
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
    statuses: [422, 429],
    edge: "BYPASS",
  },
];

if (includeTop) {
  checks.push({ name: "top", path: "/v1/leaderboard/top?window=24h", statuses: [200], edge: "cache" });
  checks.push({
    name: "explain",
    path: "/v1/leaderboard/explain?miner_hotkey=5test",
    statuses: [200],
    edge: "cache",
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function probe(check) {
  const started = Date.now();
  const res = await fetch(`${base}${check.path}`, {
    method: check.method || "GET",
    headers: check.headers || {},
    body: check.body,
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
  await res.arrayBuffer();
  return {
    name: check.name,
    path: check.path,
    status: res.status,
    ms: Date.now() - started,
    edge: res.headers.get("x-cathedral-edge-cache") || "",
    server: res.headers.get("server") || "",
    okStatus: check.statuses.includes(res.status),
    expectedEdge: check.edge,
  };
}

function percentile(values, p) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[index];
}

function validate(result) {
  const errors = [];
  if (!result.okStatus) {
    errors.push(`bad_status:${result.status}`);
  }
  if (expectWorker) {
    if (!/cloudflare/i.test(result.server)) {
      errors.push(`not_cloudflare:${result.server || "missing"}`);
    }
    if (result.expectedEdge === "cache") {
      if (!EDGE_VALUES.has(result.edge) || result.edge === "BYPASS") {
        errors.push(`bad_edge_cache:${result.edge || "missing"}`);
      }
    } else if (result.expectedEdge !== "none" && result.edge !== result.expectedEdge) {
      errors.push(`bad_edge:${result.edge || "missing"}`);
    }
  } else if (result.edge) {
    errors.push(`unexpected_edge_in_bypass:${result.edge}`);
  }
  if (result.ms > timeoutMs) {
    errors.push(`slow_over_timeout:${result.ms}`);
  }
  return errors;
}

function printSummary(results, errors) {
  const byName = new Map();
  for (const result of results) {
    if (!byName.has(result.name)) byName.set(result.name, []);
    byName.get(result.name).push(result);
  }

  console.log("");
  console.log(`edge-soak base=${base} expectWorker=${expectWorker} allowBypass=${allowBypass}`);
  console.log(`iterations=${iterations} intervalMs=${intervalMs} timeoutMs=${timeoutMs} checks=${checks.length}`);
  console.log("name      count  ok  p50ms  p95ms  maxms  edge");
  for (const [name, rows] of byName) {
    const times = rows.map((row) => row.ms);
    const edgeCounts = {};
    for (const row of rows) {
      edgeCounts[row.edge || "-"] = (edgeCounts[row.edge || "-"] || 0) + 1;
    }
    console.log(
      `${name.padEnd(9)} ${String(rows.length).padStart(5)} ` +
      `${String(rows.filter((row) => validate(row).length === 0).length).padStart(3)} ` +
      `${String(percentile(times, 50)).padStart(6)} ` +
      `${String(percentile(times, 95)).padStart(6)} ` +
      `${String(Math.max(...times)).padStart(6)}  ${JSON.stringify(edgeCounts)}`,
    );
  }
  if (errors.length) {
    console.log("");
    console.log("errors:");
    for (const error of errors.slice(0, 20)) {
      console.log(`- ${error}`);
    }
    if (errors.length > 20) console.log(`- ... ${errors.length - 20} more`);
  }
}

const results = [];
const errors = [];

for (let i = 0; i < iterations; i += 1) {
  for (const check of checks) {
    try {
      const result = await probe(check);
      results.push(result);
      const validationErrors = validate(result);
      for (const error of validationErrors) {
        errors.push(`${check.name} ${check.path} ${error} ${result.ms}ms`);
      }
      process.stdout.write(validationErrors.length ? "!" : ".");
    } catch (error) {
      const message = `${check.name} ${check.path} exception:${String(error && error.message || error)}`;
      errors.push(message);
      process.stdout.write("!");
    }
  }
  if (i < iterations - 1) await sleep(intervalMs);
}

console.log("");
printSummary(results, errors);
assert.equal(errors.length, 0, `${errors.length} soak errors`);
