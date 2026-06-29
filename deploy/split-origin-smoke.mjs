import assert from "node:assert/strict";

const readBase = (process.env.CATHEDRAL_READ_BASE_URL ||
  "https://read.cathedral.computer").replace(/\/+$/, "");
const submitBase = (process.env.CATHEDRAL_SUBMIT_BASE_URL ||
  "https://submit.cathedral.computer").replace(/\/+$/, "");
const workerBase = (process.env.CATHEDRAL_WORKER_BASE_URL || "").replace(/\/+$/, "");
const timeoutMs = Number(process.env.CATHEDRAL_SPLIT_TIMEOUT_MS || 10000);
const maxHotMs = Number(process.env.CATHEDRAL_SPLIT_MAX_HOT_MS || 5000);
const retryAttempts = Math.max(1, Number(process.env.CATHEDRAL_SPLIT_ATTEMPTS || 5));
const retryDelayMs = Math.max(0, Number(process.env.CATHEDRAL_SPLIT_RETRY_DELAY_MS || 1000));
const TRANSIENT_STATUSES = new Set([500, 502, 503, 504]);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function request(base, path, init = {}) {
  const started = Date.now();
  const res = await fetch(`${base}${path}`, {
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
    ...init,
  });
  const body = await res.text();
  return {
    base,
    path,
    method: init.method || "GET",
    status: res.status,
    ms: Date.now() - started,
    headers: res.headers,
    body,
  };
}

function line(result, label) {
  const role = result.headers.get("x-cathedral-service-role") || "-";
  console.log(
    `${label.padEnd(7)} ${result.method.padEnd(7)} ${String(result.status).padStart(3)} ` +
    `${String(result.ms).padStart(5)}ms role=${role.padEnd(7)} ${result.path}`,
  );
}

function assertStatus(result, statuses) {
  assert.ok(
    statuses.includes(result.status),
    `${result.base}${result.path} returned ${result.status}, expected ${statuses.join("/")}`,
  );
}

function assertHotLatency(result) {
  assert.ok(
    result.ms <= maxHotMs,
    `${result.base}${result.path} took ${result.ms}ms, above hot-path ceiling ${maxHotMs}ms`,
  );
}

async function requestEventually(base, path, init = {}, statuses = [200]) {
  let lastError;
  for (let attempt = 1; attempt <= retryAttempts; attempt += 1) {
    try {
      const result = await request(base, path, init);
      if (
        statuses.includes(result.status) ||
        !TRANSIENT_STATUSES.has(result.status) ||
        attempt === retryAttempts
      ) {
        return result;
      }
      console.log(
        `retry ${attempt}/${retryAttempts} ${result.status} ${path} after ${retryDelayMs}ms`,
      );
    } catch (err) {
      lastError = err;
      if (attempt === retryAttempts) throw err;
      console.log(
        `retry ${attempt}/${retryAttempts} ${path} error=${err.message} after ${retryDelayMs}ms`,
      );
    }
    await sleep(retryDelayMs);
  }
  throw lastError;
}

function json(result) {
  try {
    return JSON.parse(result.body);
  } catch (err) {
    assert.fail(`${result.base}${result.path} did not return JSON: ${err.message}`);
  }
}

function assertRole(result, expectedRole) {
  const payload = json(result);
  assert.equal(payload.service_role, expectedRole, `${result.base}${result.path} wrong service_role`);
  return payload;
}

function assertRejectedByRole(result, role) {
  assertStatus(result, [404]);
  assert.equal(result.headers.get("x-cathedral-service-role"), role);
  assert.equal(result.headers.get("x-cathedral-rejection-reason"), `route_not_served_by_${role}_role`);
  assert.match(result.body, new RegExp(`route_not_served_by_${role}_role`));
}

async function checkReadOrigin() {
  const live = await request(readBase, "/health/live");
  line(live, "read");
  assertStatus(live, [200]);
  assertHotLatency(live);
  assertRole(live, "read");

  const ready = await request(readBase, "/health/ready");
  line(ready, "read");
  assertStatus(ready, [200]);
  assertHotLatency(ready);
  const readyPayload = assertRole(ready, "read");
  assert.equal(readyPayload.db, "ok");

  for (const path of [
    "/v1/synthetic-boolean/active-challenges",
    "/v1/validator/weights/next",
    "/v1/leaderboard/recent?limit=2",
    "/v1/leaderboard/top?window=1h",
    "/v1/leaderboard/explain?miner_hotkey=5test",
    "/v1/verifiable-sat/coinbase/status",
  ]) {
    const result = await requestEventually(readBase, path);
    line(result, "read");
    assertStatus(result, [200]);
    assertHotLatency(result);
  }

  const badSubmit = await request(readBase, "/v1/agents/submit", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  line(badSubmit, "read");
  assertRejectedByRole(badSubmit, "read");
  assertHotLatency(badSubmit);
}

async function checkSubmitOrigin() {
  const live = await request(submitBase, "/health/live");
  line(live, "submit");
  assertStatus(live, [200]);
  assertHotLatency(live);
  assertRole(live, "submit");

  const ready = await request(submitBase, "/health/ready");
  line(ready, "submit");
  assertStatus(ready, [200]);
  assertHotLatency(ready);
  const readyPayload = assertRole(ready, "submit");
  assert.equal(readyPayload.db, "ok");

  for (const path of [
    "/v1/synthetic-boolean/active-cnf",
    "/v1/synthetic-boolean/per-miner/challenges",
    "/v1/verifiable-sat/coinbase/challenge",
  ]) {
    const result = await request(submitBase, path);
    line(result, "submit");
    assertStatus(result, [422, 429]);
    assertHotLatency(result);
  }

  const status = await request(submitBase, "/v1/verifiable-sat/coinbase/status");
  line(status, "submit");
  assertStatus(status, [200]);
  assertHotLatency(status);

  const badSubmit = await request(submitBase, "/v1/agents/submit", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  line(badSubmit, "submit");
  assertStatus(badSubmit, [422, 429]);
  assertHotLatency(badSubmit);

  const leaderboard = await request(submitBase, "/v1/leaderboard/top?window=1h");
  line(leaderboard, "submit");
  assertRejectedByRole(leaderboard, "submit");
  assertHotLatency(leaderboard);
}

async function checkWorkerOrigin() {
  if (!workerBase) return;

  const live = await request(workerBase, "/health/live");
  line(live, "worker");
  assertStatus(live, [200]);
  assertHotLatency(live);
  assertRole(live, "worker");

  const active = await request(workerBase, "/v1/synthetic-boolean/active-challenges");
  line(active, "worker");
  assertRejectedByRole(active, "worker");
  assertHotLatency(active);
}

console.log(`split-origin smoke read=${readBase} submit=${submitBase}${workerBase ? ` worker=${workerBase}` : ""} max_hot_ms=${maxHotMs}`);
await checkReadOrigin();
await checkSubmitOrigin();
await checkWorkerOrigin();
console.log("split-origin smoke passed");
