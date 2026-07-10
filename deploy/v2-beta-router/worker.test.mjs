import assert from "node:assert/strict";

import worker from "./worker.mjs";

const DOGFOOD_HOTKEY = "5H1DGfCH5A6sRxiA64xdTG4SSpf7HpXtqGB8bohqnU1MWqv4";
const NON_CANARY_HOTKEY = "5NotACanaryHotkeyAtAll1111111111111111111111111";

let originCalls = [];
let originHandler = async () => new Response("origin-ok", {
  status: 200,
  headers: { "x-origin-test": "hit" },
});

globalThis.fetch = async (url, init = {}) => {
  originCalls.push({ url: String(url), init });
  return originHandler(url, init);
};

class TestCache {
  constructor() {
    this.entries = new Map();
    this.puts = [];
  }

  key(request) {
    return `${request.method} ${request.url}`;
  }

  async match(request) {
    const response = this.entries.get(this.key(request));
    return response?.clone();
  }

  async put(request, response) {
    this.puts.push({ request: request.clone(), response: response.clone() });
    this.entries.set(this.key(request), response.clone());
  }
}

function minerRequest(path, { method = "GET", hotkey = NON_CANARY_HOTKEY } = {}) {
  return new Request(`https://v2-beta.cathedral.computer${path}`, {
    method,
    headers: {
      "x-cathedral-hotkey": hotkey,
      "x-cathedral-signature": "test-signature",
      "x-cathedral-submitted-at": "2026-07-08T09:55:00.000Z",
    },
  });
}

function resetOriginCalls() {
  originCalls = [];
  originHandler = async () => new Response("origin-ok", {
    status: 200,
    headers: { "x-origin-test": "hit" },
  });
}

function receiptPayload(receiptId, terminal = false) {
  return {
    schema: "cathedral.v2.submit_bitset_receipt.v1",
    receipt_id: receiptId,
    status: terminal ? "verified" : "received",
    terminal,
    open: !terminal,
    miner_hotkey: "5ReceiptOwner",
  };
}

async function json(response) {
  return JSON.parse(await response.text());
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v2/synthetic-boolean/per-miner/challenges?limit=50")
  );
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("x-cathedral-rejection-reason"), "v2_beta_staged_reopen");
  assert.equal(response.headers.get("x-cathedral-v2-beta-origin"), "edge-staged-reopen");
  assert.equal((await json(response)).reason, "v2_beta_staged_reopen");
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  const response = await worker.fetch(
    new Request("https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/challenges")
  );
  assert.equal(response.status, 422);
  assert.equal(response.headers.get("x-cathedral-v2-beta-origin"), "edge-preflight");
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v2/synthetic-boolean/per-miner/challenges?limit=50", {
      hotkey: DOGFOOD_HOTKEY,
    })
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-cathedral-v2-beta-origin"), "polaris-sandbox");
  assert.equal(originCalls.length, 1);
  const forwardedUrl = new URL(originCalls[0].url);
  assert.equal(forwardedUrl.protocol, "http:");
  assert.equal(forwardedUrl.hostname, "sandbox-v2-origin.cathedral.computer");
  assert.equal(forwardedUrl.port, "8080");
  assert.equal(forwardedUrl.searchParams.get("limit"), "10");
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" })
  );
  assert.equal(response.status, 429);
  assert.equal(response.headers.get("x-cathedral-rejection-reason"), "v2_beta_staged_reopen");
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v1/synthetic-boolean/per-miner/challenges?limit=1")
  );
  assert.equal(response.status, 410);
  assert.equal(response.headers.get("x-cathedral-rejection-reason"), "v1_miner_path_retired");
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v1/synthetic-boolean/per-miner/challenges?limit=1", {
      hotkey: DOGFOOD_HOTKEY,
    }),
    { V2_GATE_MODE: "open-v2" }
  );
  assert.equal(response.status, 410);
  assert.equal(response.headers.get("x-cathedral-rejection-reason"), "v1_miner_path_retired");
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v2/synthetic-boolean/per-miner/challenges?limit=50"),
    { V2_GATE_MODE: "open-v2" }
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-cathedral-v2-beta-origin"), "polaris-sandbox");
  assert.equal(originCalls.length, 1);
  const forwardedUrl = new URL(originCalls[0].url);
  assert.equal(forwardedUrl.searchParams.get("limit"), "10");
}

resetOriginCalls();
{
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" }),
    { V2_GATE_MODE: "open-v2" }
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-cathedral-v2-beta-origin"), "polaris-sandbox");
  assert.equal(originCalls.length, 1);
}

// ---- Percentage ramp (V2_OPEN_PERCENT while staged) ----
// FNV-1a buckets: NON_CANARY_HOTKEY=81, LOW_BUCKET_HOTKEY=46.
const LOW_BUCKET_HOTKEY = "5LowBucketTestHotkeyAAAAAAAAAAAAAAAAAAAAAAAA";

resetOriginCalls();
{
  // percent unset -> pure staged, non-canary rejected
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" }),
    {}
  );
  assert.equal(response.status, 429);
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  // percent=50 admits bucket 46, rejects bucket 81 - stable slice
  const admitted = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: LOW_BUCKET_HOTKEY }),
    { V2_OPEN_PERCENT: "50" }
  );
  assert.equal(admitted.status, 200);
  assert.equal(originCalls.length, 1);
  const rejected = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" }),
    { V2_OPEN_PERCENT: "50" }
  );
  assert.equal(rejected.status, 429);
  assert.equal((await json(rejected)).reason, "v2_beta_staged_reopen");
  assert.equal(originCalls.length, 1);
}

resetOriginCalls();
{
  // percent=100 admits everyone even while staged
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" }),
    { V2_OPEN_PERCENT: "100" }
  );
  assert.equal(response.status, 200);
  assert.equal(originCalls.length, 1);
}

resetOriginCalls();
{
  // garbage/zero/negative percent -> staged
  for (const percent of ["", "abc", "0", "-5"]) {
    const response = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset", { method: "POST" }),
      { V2_OPEN_PERCENT: percent }
    );
    assert.equal(response.status, 429);
  }
  assert.equal(originCalls.length, 0);
}

resetOriginCalls();
{
  // canary still admitted at percent=0
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: DOGFOOD_HOTKEY }),
    { V2_OPEN_PERCENT: "0" }
  );
  assert.equal(response.status, 200);
  assert.equal(originCalls.length, 1);
}

// ---- edge per-miner rate limit ---------------------------------------------

resetOriginCalls();
{
  // burst passes, then 429 edge_rate_limited; origin only sees the burst
  const env = { V2_OPEN_PERCENT: "100", V2_EDGE_MINER_RPS: "1", V2_EDGE_MINER_BURST: "3" };
  const hk = "5RateLimitTestMinerAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  let statuses = [];
  for (let i = 0; i < 5; i++) {
    const r = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env);
    statuses.push(r.status);
  }
  assert.deepEqual(statuses, [200, 200, 200, 429, 429]);
  const last = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env);
  assert.equal((await json(last)).reason, "edge_rate_limited");
  assert.equal(last.headers.get("retry-after"), "2");
  assert.equal(originCalls.length, 3);
}

resetOriginCalls();
{
  // receipt polls are rate limited too (they bypass the staged gate)
  const env = { V2_EDGE_MINER_RPS: "1", V2_EDGE_MINER_BURST: "2" };
  const hk = "5ReceiptPollTestMinerBBBBBBBBBBBBBBBBBBBBBBBBBB";
  let statuses = [];
  for (let i = 0; i < 4; i++) {
    const r = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset/receipts/abc123", { hotkey: hk }), env);
    statuses.push(r.status);
  }
  assert.deepEqual(statuses, [200, 200, 429, 429]);
  assert.equal(originCalls.length, 2);
}

resetOriginCalls();
{
  // canary hotkeys are exempt (keeps E2E smoke + edge soak working)
  const env = { V2_EDGE_MINER_RPS: "1", V2_EDGE_MINER_BURST: "2" };
  for (let i = 0; i < 10; i++) {
    const r = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: DOGFOOD_HOTKEY }), env);
    assert.equal(r.status, 200);
  }
  assert.equal(originCalls.length, 10);
}

resetOriginCalls();
{
  // V2_EDGE_MINER_RPS=0 disables the limiter entirely
  const env = { V2_OPEN_PERCENT: "100", V2_EDGE_MINER_RPS: "0" };
  const hk = "5DisabledLimiterTestMinerCCCCCCCCCCCCCCCCCCCCCC";
  for (let i = 0; i < 20; i++) {
    const r = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env);
    assert.equal(r.status, 200);
  }
  assert.equal(originCalls.length, 20);
}

resetOriginCalls();
{
  // tokens refill over time (stubbed clock)
  const env = { V2_OPEN_PERCENT: "100", V2_EDGE_MINER_RPS: "2", V2_EDGE_MINER_BURST: "2" };
  const hk = "5RefillTestMinerDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD";
  const realNow = Date.now;
  let t = realNow();
  Date.now = () => t;
  try {
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 200);
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 200);
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 429);
    t += 1000; // +1s at 2 rps -> 2 tokens back
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 200);
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 200);
    assert.equal((await worker.fetch(minerRequest("/v2/agents/submit-bitset", { method: "POST", hotkey: hk }), env)).status, 429);
  } finally {
    Date.now = realNow;
  }
}

resetOriginCalls();
{
  // staged gate still wins: non-admitted miner gets staged 429, not rate-limit 429
  const env = { V2_EDGE_MINER_RPS: "1", V2_EDGE_MINER_BURST: "1" };
  const r = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset", { method: "POST" }), env);
  assert.equal(r.status, 429);
  assert.equal((await json(r)).reason, "v2_beta_staged_reopen");
  assert.equal(originCalls.length, 0);
}

// ---- receipt cache --------------------------------------------------------

resetOriginCalls();
{
  // A validated pending receipt is stored for two seconds and the next poll is
  // served without an origin read, while client-facing no-store is preserved.
  const cache = new TestCache();
  originHandler = async () => Response.json(receiptPayload("pending-1"), {
    headers: { "x-cathedral-receipt-signature": "signed-test-value" },
  });
  const env = { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" };
  const path = "/v2/agents/submit-bitset/receipts/pending-1?ignored=one";
  const miss = await worker.fetch(minerRequest(path), env);
  assert.equal(miss.status, 200);
  assert.equal(miss.headers.get("x-cathedral-v2-beta-cache"), "MISS");
  assert.equal(miss.headers.get("cache-control"), "no-store");
  assert.equal(miss.headers.get("x-cathedral-receipt-signature"), "signed-test-value");
  assert.equal(cache.puts.length, 1);
  assert.equal(cache.puts[0].response.headers.get("cache-control"), "public, max-age=2");
  assert.equal(cache.puts[0].response.headers.get("x-cathedral-submit-token"), null);

  const hit = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset/receipts/pending-1?ignored=two", {
      hotkey: "5DifferentPollingMiner",
    }), env);
  assert.equal(hit.status, 200);
  assert.equal(hit.headers.get("x-cathedral-v2-beta-cache"), "HIT");
  assert.equal(hit.headers.get("cache-control"), "no-store");
  assert.equal(hit.headers.get("x-cathedral-receipt-signature"), "signed-test-value");
  assert.equal((await json(hit)).receipt_id, "pending-1");
  assert.equal(originCalls.length, 1);
}

resetOriginCalls();
{
  const cache = new TestCache();
  originHandler = async () => Response.json(receiptPayload("terminal-1", true));
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset/receipts/terminal-1"),
    { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" });
  assert.equal(response.headers.get("x-cathedral-v2-beta-cache"), "MISS");
  assert.equal(cache.puts[0].response.headers.get("cache-control"), "public, max-age=300");
}

resetOriginCalls();
{
  // Errors and malformed or identity-mismatched JSON never enter the cache.
  for (const responseFactory of [
    () => Response.json({ detail: "busy" }, { status: 503, headers: { "retry-after": "7" } }),
    () => new Response("not-json", { status: 200 }),
    () => Response.json(receiptPayload("some-other-receipt")),
  ]) {
    const cache = new TestCache();
    originHandler = async () => responseFactory();
    const response = await worker.fetch(
      minerRequest("/v2/agents/submit-bitset/receipts/not-cacheable"),
      { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" });
    assert.equal(response.headers.get("x-cathedral-v2-beta-cache"), "BYPASS");
    assert.equal(cache.puts.length, 0);
  }
}

resetOriginCalls();
{
  // Any token-bearing response is rejected wholesale rather than sanitized.
  const cache = new TestCache();
  originHandler = async () => Response.json(receiptPayload("sensitive-1"), {
    headers: { "x-cathedral-submit-token": "must-never-be-stored" },
  });
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset/receipts/sensitive-1"),
    { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" });
  assert.equal(response.headers.get("x-cathedral-v2-beta-cache"), "BYPASS");
  assert.equal(cache.puts.length, 0);
}

resetOriginCalls();
{
  // Receipt identity is part of the cache key; one receipt cannot satisfy
  // another receipt's poll.
  const cache = new TestCache();
  originHandler = async (url) => {
    const id = new URL(url).pathname.split("/").at(-1);
    return Response.json(receiptPayload(id));
  };
  const env = { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" };
  const a = await worker.fetch(minerRequest("/v2/agents/submit-bitset/receipts/isolated-a"), env);
  const b = await worker.fetch(minerRequest("/v2/agents/submit-bitset/receipts/isolated-b"), env);
  assert.equal((await json(a)).receipt_id, "isolated-a");
  assert.equal((await json(b)).receipt_id, "isolated-b");
  assert.equal(originCalls.length, 2);
  assert.equal(cache.puts.length, 2);
}

resetOriginCalls();
{
  // Cache outages fail open after the origin response is available.
  const cache = new TestCache();
  cache.match = async () => { throw new Error("cache match unavailable"); };
  cache.put = async () => { throw new Error("cache put unavailable"); };
  originHandler = async () => Response.json(receiptPayload("cache-error-1"));
  const response = await worker.fetch(
    minerRequest("/v2/agents/submit-bitset/receipts/cache-error-1"),
    { RECEIPT_CACHE: cache, V2_EDGE_MINER_RPS: "0" });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-cathedral-v2-beta-cache"), "ERROR");
  assert.equal((await json(response)).receipt_id, "cache-error-1");
  assert.equal(originCalls.length, 1);
}

console.log("v2-beta-router edge rate limit tests passed");

console.log("v2-beta-router staged/open gate tests passed");
