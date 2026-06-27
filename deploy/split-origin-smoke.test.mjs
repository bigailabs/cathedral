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

function text(res, status, body, headers = {}) {
  res.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    ...headers,
  });
  res.end(body);
}

function roleReject(res, role) {
  const reason = `route_not_served_by_${role}_role`;
  text(res, 404, reason, {
    "x-cathedral-service-role": role,
    "x-cathedral-rejection-reason": reason,
  });
}

async function serve(handler) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise((resolve, reject) => server.close((err) => err ? reject(err) : resolve())),
  };
}

function makeReadServer({ acceptSubmit = false } = {}) {
  return serve((req, res) => {
    if (req.url === "/health/live") return json(res, 200, { service_role: "read", db: "not_checked" });
    if (req.url === "/health/ready") return json(res, 200, { service_role: "read", db: "ok" });
    if (req.method === "POST" && req.url === "/v1/agents/submit") {
      if (acceptSubmit) return json(res, 200, { status: "accepted_by_wrong_origin" });
      return roleReject(res, "read");
    }
    if ([
      "/v1/synthetic-boolean/active-challenges",
      "/v1/validator/weights/next",
      "/v1/leaderboard/recent?limit=2",
      "/v1/leaderboard/top?window=1h",
      "/v1/leaderboard/explain?miner_hotkey=5test",
      "/v1/verifiable-sat/coinbase/status",
    ].includes(req.url)) {
      return json(res, 200, { ok: true });
    }
    text(res, 404, "not_found");
  });
}

function makeSubmitServer({ serveLeaderboard = false } = {}) {
  return serve((req, res) => {
    if (req.url === "/health/live") return json(res, 200, { service_role: "submit", db: "not_checked" });
    if (req.url === "/health/ready") return json(res, 200, { service_role: "submit", db: "ok" });
    if (req.url === "/v1/verifiable-sat/coinbase/status") return json(res, 200, { ok: true });
    if (req.url === "/v1/leaderboard/top?window=1h") {
      if (serveLeaderboard) return json(res, 200, { status: "served_by_wrong_origin" });
      return roleReject(res, "submit");
    }
    if ([
      "/v1/synthetic-boolean/active-cnf",
      "/v1/synthetic-boolean/per-miner/challenges",
      "/v1/verifiable-sat/coinbase/challenge",
      "/v1/agents/submit",
    ].includes(req.url)) {
      return json(res, 422, { error: "expected_unsigned_test_request" });
    }
    text(res, 404, "not_found");
  });
}

function makeWorkerServer() {
  return serve((req, res) => {
    if (req.url === "/health/live") return json(res, 200, { service_role: "worker", db: "not_checked" });
    if (req.url === "/v1/synthetic-boolean/active-challenges") return roleReject(res, "worker");
    text(res, 404, "not_found");
  });
}

async function runSmoke(env) {
  return await new Promise((resolve) => {
    const child = spawn(process.execPath, ["deploy/split-origin-smoke.mjs"], {
      env: {
        ...process.env,
        ...env,
        CATHEDRAL_SPLIT_TIMEOUT_MS: "2000",
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

async function withServers(options, fn) {
  const read = await makeReadServer(options);
  const submit = await makeSubmitServer(options);
  const worker = await makeWorkerServer();
  try {
    return await fn({ read, submit, worker });
  } finally {
    await Promise.all([read.close(), submit.close(), worker.close()]);
  }
}

await withServers({}, async ({ read, submit, worker }) => {
  const result = await runSmoke({
    CATHEDRAL_READ_BASE_URL: read.url,
    CATHEDRAL_SUBMIT_BASE_URL: submit.url,
    CATHEDRAL_WORKER_BASE_URL: worker.url,
  });
  assert.equal(result.code, 0, result.stderr || result.stdout);
  assert.match(result.stdout, /split-origin smoke passed/);
});

await withServers({ acceptSubmit: true }, async ({ read, submit, worker }) => {
  const result = await runSmoke({
    CATHEDRAL_READ_BASE_URL: read.url,
    CATHEDRAL_SUBMIT_BASE_URL: submit.url,
    CATHEDRAL_WORKER_BASE_URL: worker.url,
  });
  assert.notEqual(result.code, 0, "smoke should fail when read origin accepts submit traffic");
  assert.match(result.stderr + result.stdout, /returned 200, expected 404/);
});

await withServers({ serveLeaderboard: true }, async ({ read, submit, worker }) => {
  const result = await runSmoke({
    CATHEDRAL_READ_BASE_URL: read.url,
    CATHEDRAL_SUBMIT_BASE_URL: submit.url,
    CATHEDRAL_WORKER_BASE_URL: worker.url,
  });
  assert.notEqual(result.code, 0, "smoke should fail when submit origin serves leaderboard reads");
  assert.match(result.stderr + result.stdout, /returned 200, expected 404/);
});

console.log("split-origin smoke tests passed");
