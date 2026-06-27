import assert from "node:assert/strict";
import {
  canonicalPath,
  handleRequest,
  originTimeoutMs,
  serveStale,
} from "./worker.js";

// --- A minimal in-memory KV mock matching the slice of the Workers KV API the
//     worker uses: put(key, value, {metadata}) and getWithMetadata(key). ---
function mockKV(initial = null) {
  const store = initial ? new Map([[ "lkg:weights_next", initial ]]) : new Map();
  return {
    store,
    puts: [],
    async put(key, value, opts) {
      this.puts.push({ key, value, opts });
      store.set(key, { value, metadata: (opts && opts.metadata) || null });
    },
    async getWithMetadata(key) {
      return store.get(key) || { value: null, metadata: null };
    },
  };
}

const SIGNED = JSON.stringify({
  vector_id: "v-1",
  generated_at: "2026-06-27T00:00:00.000Z",
  expires_at: "2026-06-27T00:30:00.000Z",
  signature: "sig",
});

function ctx() {
  const pending = [];
  return { pending, waitUntil: (p) => pending.push(p) };
}

const ENV_BASE = { PUBLISHER_ORIGIN: "https://publisher.example" };

// --- canonicalPath strips the legacy /api/cathedral prefix and nothing else ---
assert.equal(canonicalPath("/v1/validator/weights/next"), "/v1/validator/weights/next");
assert.equal(
  canonicalPath("/api/cathedral/v1/validator/weights/next"),
  "/v1/validator/weights/next",
);
assert.equal(canonicalPath("/api/cathedral"), "/");

// --- originTimeoutMs: default + env override ---
assert.equal(originTimeoutMs({}), 8000);
assert.equal(originTimeoutMs({ WEIGHTS_ORIGIN_TIMEOUT_MS: "3000" }), 3000);
assert.equal(originTimeoutMs({ WEIGHTS_ORIGIN_TIMEOUT_MS: "" }), 8000);

// --- origin 2xx stores the signed body in KV and serves it from origin with a
//     short fresh-vector edge cache (NOT no-store) ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "https://publisher.example/v1/validator/weights/next");
    assert.equal(init.method, "GET");
    return new Response(SIGNED, { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"),
      env,
      c,
    );
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "origin");
    // The signed body is served verbatim.
    assert.equal(await resp.text(), SIGNED);
    // Edge caching restored: short shared-cache window + stale-while-revalidate.
    const cc = resp.headers.get("cache-control");
    assert.match(cc, /s-maxage=15/);
    assert.match(cc, /stale-while-revalidate=1200/);
    assert.doesNotMatch(cc, /no-store/);
    // KV got the LKG write (via waitUntil).
    await Promise.all(c.pending);
    assert.equal(kv.puts.length, 1);
    assert.equal(kv.puts[0].value, SIGNED);
    assert.ok(kv.puts[0].opts.metadata.stored_at);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- origin 5xx serves the LKG body from KV with stale_fallback marker ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("upstream boom", { status: 500 });
  try {
    const kv = mockKV({ value: SIGNED, metadata: { stored_at: "2026-06-27T00:01:00.000Z" } });
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"),
      env,
      ctx(),
    );
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "stale_fallback");
    assert.equal(resp.headers.get("x-cathedral-fallback-reason"), "origin_status_500");
    assert.equal(resp.headers.get("x-cathedral-vector-stored-at"), "2026-06-27T00:01:00.000Z");
    // Stale fallback must NOT be re-cached at the edge as if fresh.
    assert.equal(resp.headers.get("cache-control"), "no-store");
    // Served body is the original signed bytes, verbatim.
    assert.equal(await resp.text(), SIGNED);
    // 5xx does NOT overwrite the LKG.
    assert.equal(kv.puts.length, 0);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- origin timeout / abort serves LKG (origin_error) ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) =>
    new Promise((_resolve, reject) => {
      // Simulate an AbortController abort firing.
      if (init && init.signal) {
        init.signal.addEventListener("abort", () =>
          reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
      }
    });
  try {
    const kv = mockKV({ value: SIGNED, metadata: { stored_at: "2026-06-27T00:02:00.000Z" } });
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv, WEIGHTS_ORIGIN_TIMEOUT_MS: "10" };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"),
      env,
      ctx(),
    );
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "stale_fallback");
    assert.equal(resp.headers.get("x-cathedral-fallback-reason"), "origin_error");
    assert.equal(await resp.text(), SIGNED);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- origin down AND KV empty -> 503 no vector available ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("connect timeout");
  };
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"),
      env,
      ctx(),
    );
    assert.equal(resp.status, 503);
    const body = await resp.json();
    assert.equal(body.error, "no vector available");
    assert.equal(body.reason, "origin_error");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- /api/cathedral prefix is stripped before forwarding to the origin ---
{
  const realFetch = globalThis.fetch;
  let seenUrl = null;
  globalThis.fetch = async (url) => {
    seenUrl = url;
    return new Response(SIGNED, { status: 200 });
  };
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    await handleRequest(
      new Request("https://api.cathedral.computer/api/cathedral/v1/validator/weights/next?x=1"),
      env,
      ctx(),
    );
    assert.equal(seenUrl, "https://publisher.example/v1/validator/weights/next?x=1");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- body.length > 2 guard: a trivial body ("{}" or empty) is NOT stored as LKG ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("{}", { status: 200 });
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"),
      env,
      c,
    );
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "origin");
    await Promise.all(c.pending);
    // "{}" has length 2 -> guard rejects it, KV stays empty.
    assert.equal(kv.puts.length, 0);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- serveStale is directly callable and returns the stored vector ---
{
  const kv = mockKV({ value: SIGNED, metadata: { stored_at: "2026-06-27T00:03:00.000Z" } });
  const resp = await serveStale({ WEIGHTS_LKG: kv }, "manual");
  assert.equal(resp.status, 200);
  assert.equal(resp.headers.get("x-cathedral-fallback-reason"), "manual");
  assert.equal(await resp.text(), SIGNED);
}

console.log("weights-failover worker tests passed");
