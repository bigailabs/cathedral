import assert from "node:assert/strict";

import worker from "./worker.mjs";

const DOGFOOD_HOTKEY = "5H1DGfCH5A6sRxiA64xdTG4SSpf7HpXtqGB8bohqnU1MWqv4";
const NON_CANARY_HOTKEY = "5NotACanaryHotkeyAtAll1111111111111111111111111";

let originCalls = [];

globalThis.fetch = async (url, init = {}) => {
  originCalls.push({ url: String(url), init });
  return new Response("origin-ok", {
    status: 200,
    headers: { "x-origin-test": "hit" },
  });
};

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

console.log("v2-beta-router staged/open gate tests passed");
