import assert from "node:assert/strict";

const base = (process.env.CATHEDRAL_EDGE_BASE_URL ||
  "https://cathedral-edge-router.fredesere.workers.dev").replace(/\/+$/, "");
const isWorkersDev = base.includes("workers.dev");
const baseHost = new URL(base).hostname;
const isProductionApi = baseHost === "api.cathedral.computer";
const allowBypass = process.env.CATHEDRAL_EDGE_ALLOW_BYPASS === "1";
const expectWorker = (
  isWorkersDev ||
  process.env.CATHEDRAL_EDGE_EXPECT_WORKER === "1" ||
  (isProductionApi && !allowBypass)
);

async function request(path, init = {}) {
  const started = Date.now();
  const res = await fetch(`${base}${path}`, {
    redirect: "manual",
    ...init,
  });
  const body = await res.text();
  return {
    path,
    status: res.status,
    ms: Date.now() - started,
    headers: res.headers,
    body,
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function line(result) {
  const edge = result.headers.get("x-cathedral-edge-cache") || "-";
  console.log(`${result.status} ${String(result.ms).padStart(4)}ms ${edge.padEnd(13)} ${result.path}`);
}

const ready = await request("/health/ready");
line(ready);
assert.equal(ready.status, 200);
if (expectWorker) {
  assert.match(ready.body, /"service_role":"read"/);
  assert.equal(ready.headers.get("x-cathedral-edge-cache"), "BYPASS");
}

const active1 = await request("/v1/synthetic-boolean/active-challenges");
line(active1);
assert.equal(active1.status, 200);
if (expectWorker) {
  assert.match(active1.headers.get("cache-control") || "", /s-maxage=60/);
  assert.ok(active1.headers.get("x-cathedral-edge-cache"));
}

const active2 = await request("/v1/synthetic-boolean/active-challenges");
line(active2);
assert.equal(active2.status, 200);
if (expectWorker) {
  let sawHit = active2.headers.get("x-cathedral-edge-cache") === "HIT";
  for (let i = 0; !sawHit && i < 5; i += 1) {
    await sleep(250);
    const retry = await request("/v1/synthetic-boolean/active-challenges");
    line(retry);
    assert.equal(retry.status, 200);
    sawHit = retry.headers.get("x-cathedral-edge-cache") === "HIT";
  }
  assert.equal(sawHit, true);
}

const legacyActive = await request("/api/cathedral/v1/synthetic-boolean/active-challenges");
line(legacyActive);
assert.equal(legacyActive.status, 200);
if (expectWorker) {
  assert.ok(legacyActive.headers.get("x-cathedral-edge-cache"));
}

const noisyActive = await request("/v1/synthetic-boolean/active-challenges?_=smoke");
line(noisyActive);
assert.equal(noisyActive.status, 200);

const weights = await request("/v1/validator/weights/next");
line(weights);
assert.equal(weights.status, 200);
assert.match(weights.body, /"weights"/);

const submitPm = await request("/v1/synthetic-boolean/per-miner/challenges");
line(submitPm);
assert.ok([422, 429].includes(submitPm.status));

const cacheBust = await request("/v1/leaderboard/top?x=random");
line(cacheBust);
if (expectWorker) {
  assert.equal(cacheBust.status, 400);
  assert.match(cacheBust.body, /unsupported_cache_query_param/);
}

const unknown = await request("/v1/unknown");
line(unknown);
if (isWorkersDev || process.env.CATHEDRAL_EDGE_EXPECT_UNKNOWN_DENY === "1") {
  assert.equal(unknown.status, 404);
  assert.match(unknown.body, /route_not_served_by_cathedral_edge/);
}

if (expectWorker && !isWorkersDev) {
  const readinessProbe = await request("/v1/synthetic-boolean/readiness-probe");
  line(readinessProbe);
  assert.equal(readinessProbe.status, 200);
  assert.equal(readinessProbe.headers.get("x-cathedral-edge-cache"), null);
}

const options = await request("/v1/agents/submit", { method: "OPTIONS" });
line(options);
if (expectWorker) {
  assert.equal(options.status, 204);
  assert.match(options.headers.get("access-control-allow-headers") || "", /x-cathedral-signature/);
}

const submitPost = await request("/v1/agents/submit", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: "{}",
});
line(submitPost);
assert.equal(submitPost.status, 422);
if (expectWorker) {
  assert.equal(submitPost.headers.get("x-cathedral-edge-cache"), "BYPASS");
  assert.match(submitPost.body, /x-cathedral-hotkey|Field required|solution_missing/);
}

console.log(`edge-router smoke passed for ${base}`);
