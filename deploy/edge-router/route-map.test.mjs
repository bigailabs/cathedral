import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";

function json(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
    ...headers,
  });
  res.end(body);
}

function statusFor(method, url) {
  if (method === "OPTIONS" && url === "/v1/agents/submit") return 405;
  if (method === "POST" && [
    "/v1/agents/submit",
    "/v1/verifiable-sat/coinbase/verify",
    "/v1/verifiable-sat/coinbase/submit",
  ].includes(url)) return 422;
  if ([
    "/v1/synthetic-boolean/active-cnf",
    "/v1/synthetic-boolean/per-miner/challenges",
    "/v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-test",
    "/v1/synthetic-boolean/per-miner/status",
    "/v1/verifiable-sat/coinbase/challenge",
    "/api/cathedral/v1/verifiable-sat/coinbase/challenge",
    "/v1/tee-gpu/offers",
  ].includes(url)) return 422;
  if (url === "/v1/audit-scanner/leaderboard") return 404;
  return 200;
}

async function serve({ transientRecentOnce = false } = {}) {
  let recentHits = 0;
  const server = createServer((req, res) => {
    if (req.url === "/v1/leaderboard/recent?limit=2") {
      recentHits += 1;
      if (transientRecentOnce && recentHits === 1) {
        return json(res, 503, { error: "warming" });
      }
    }
    const payload = req.url === "/health/ready"
      ? { service_role: "read", db: "ok" }
      : { ok: true };
    json(res, statusFor(req.method || "GET", req.url || ""), payload);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((resolve, reject) => server.close((err) => err ? reject(err) : resolve())),
  };
}

function runRouteMap(baseUrl) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, ["deploy/edge-router/route-map.mjs"], {
      env: {
        ...process.env,
        CATHEDRAL_EDGE_BASE_URL: baseUrl,
        CATHEDRAL_EDGE_ALLOW_BYPASS: "1",
        CATHEDRAL_EDGE_TIMEOUT_MS: "2000",
        CATHEDRAL_ROUTE_MAP_ATTEMPTS: "3",
        CATHEDRAL_ROUTE_MAP_RETRY_DELAY_MS: "10",
      },
      cwd: process.cwd(),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

const server = await serve({ transientRecentOnce: true });
try {
  const result = await runRouteMap(server.url);
  assert.equal(result.code, 0, result.stderr || result.stdout);
  assert.match(result.stdout, /retry 1\/3 503 \/v1\/leaderboard\/recent\?limit=2/);
  assert.match(result.stdout, /origin-direct route map passed/);
} finally {
  await server.close();
}

console.log("edge-router route-map tests passed");
