import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";

function json(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
    server: "cloudflare",
    ...headers,
  });
  res.end(body);
}

function headersFor(method, url, staleWeights) {
  if (url === "/health/ready") return { "x-cathedral-edge-cache": "BYPASS" };
  if (url === "/v1/verifiable-sat/coinbase/status") return { "x-cathedral-edge-cache": "BYPASS" };
  if (method === "POST" && url === "/v1/agents/submit") return { "x-cathedral-edge-cache": "BYPASS" };
  if (url === "/v1/validator/weights/next" && staleWeights) {
    return {
      "x-cathedral-edge-cache": "STALE",
      "x-cathedral-stale-fallback": "1",
    };
  }
  return { "x-cathedral-edge-cache": "HIT" };
}

function statusFor(method, url) {
  if (method === "POST" && url === "/v1/agents/submit") return 422;
  return 200;
}

async function serve({ staleWeights = false, slowReadyMs = 0 } = {}) {
  const server = createServer((req, res) => {
    const method = req.method || "GET";
    const url = req.url || "";
    if (slowReadyMs && url === "/health/ready") {
      setTimeout(() => json(res, statusFor(method, url), { ok: true }, headersFor(method, url, staleWeights)), slowReadyMs);
      return;
    }
    json(res, statusFor(method, url), { ok: true }, headersFor(method, url, staleWeights));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((resolve, reject) => server.close((err) => err ? reject(err) : resolve())),
  };
}

function runSoak(baseUrl, envOverrides = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, ["deploy/edge-router/soak.mjs"], {
      env: {
        ...process.env,
        CATHEDRAL_EDGE_BASE_URL: baseUrl,
        CATHEDRAL_EDGE_SOAK_ITERATIONS: "1",
        CATHEDRAL_EDGE_SOAK_INTERVAL_MS: "10",
        CATHEDRAL_EDGE_TIMEOUT_MS: "2000",
        CATHEDRAL_EDGE_MAX_HOT_MS: "1000",
        ...envOverrides,
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

for (const staleWeights of [false, true]) {
  const server = await serve({ staleWeights });
  try {
    const result = await runSoak(server.url);
    if (staleWeights) {
      assert.notEqual(result.code, 0, "soak should fail when weights use stale fallback");
      assert.match(result.stdout + result.stderr, /stale_weight_fallback/);
    } else {
      assert.equal(result.code, 0, result.stderr || result.stdout);
      assert.match(result.stdout, /edge-soak base=/);
    }
  } finally {
    await server.close();
  }
}

{
  const server = await serve({ slowReadyMs: 75 });
  try {
    const result = await runSoak(server.url, { CATHEDRAL_EDGE_MAX_HOT_MS: "25" });
    assert.notEqual(result.code, 0, "soak should fail when a hot endpoint is too slow");
    assert.match(result.stdout + result.stderr, /slow_hot_path/);
  } finally {
    await server.close();
  }
}

console.log("edge-router soak tests passed");
