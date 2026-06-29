import assert from "node:assert/strict";
import {
  canonicalPath,
  handleRequest,
  maybeAdvance,
  originTimeoutMs,
  parseGenMs,
  putArtifact,
  serveStale,
  vectorIsFresh,
} from "./worker.js";

// --- minimal in-memory KV mock: put(key,value,{metadata}) + getWithMetadata ---
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

// Deterministic clock: "now" = 2026-06-28T19:10:00Z.
const NOW_MS = Date.parse("2026-06-28T19:10:00.000Z");

function vec(gen, exp) {
  return JSON.stringify({ vector_id: "v-" + gen, generated_at: gen, expires_at: exp, signature: "s" });
}
// OLD: fresh, older generation. NEW: fresh, newer generation. EXPIRED: stale.
const OLD = vec("2026-06-28T18:49:00.000Z", "2026-06-28T19:30:00.000Z");
const NEW = vec("2026-06-28T19:00:00.000Z", "2026-06-28T19:40:00.000Z");
const EXPIRED = vec("2026-06-28T18:00:00.000Z", "2026-06-28T18:30:00.000Z");
const NO_EXP = JSON.stringify({ vector_id: "v-x", generated_at: "2026-06-28T19:00:00.000Z" });
const MALFORMED = "<html>oops</html>";

function entry(body, storedAt) {
  let gen;
  try { gen = JSON.parse(body).generated_at; } catch (_e) { gen = undefined; }
  return { value: body, metadata: { stored_at: storedAt, generated_at: gen } };
}

function ctx() {
  const pending = [];
  return { pending, waitUntil: (p) => pending.push(p) };
}

const ENV_BASE = {
  PUBLISHER_ORIGIN: "https://publisher.example",
  WEIGHTS_NOW_MS: String(NOW_MS),
};

function withFetch(fn, body, status = 200) {
  return async (...args) => {
    if (typeof body === "function") return body(...args);
    return new Response(body, { status });
  };
}

// ---- pure helpers ----
assert.equal(vectorIsFresh(OLD, NOW_MS), true);
assert.equal(vectorIsFresh(EXPIRED, NOW_MS), false);
assert.equal(vectorIsFresh(NO_EXP, NOW_MS), false);
assert.equal(vectorIsFresh(MALFORMED, NOW_MS), false);
assert.equal(vectorIsFresh("{}", NOW_MS), false);

assert.equal(parseGenMs(NEW, null), Date.parse("2026-06-28T19:00:00.000Z"));
assert.equal(parseGenMs(MALFORMED, null), -Infinity);
assert.equal(parseGenMs("x", { generated_at: "2026-06-28T19:00:00.000Z" }),
  Date.parse("2026-06-28T19:00:00.000Z"));

assert.equal(canonicalPath("/api/cathedral/v1/validator/weights/next"), "/v1/validator/weights/next");
assert.equal(originTimeoutMs({}), 8000);
assert.equal(originTimeoutMs({ WEIGHTS_ORIGIN_TIMEOUT_MS: "3000" }), 3000);

// ---- putArtifact monotonicity ----
{
  const kv = mockKV(entry(OLD, "t"));
  const wrote = await putArtifact({ ...ENV_BASE, WEIGHTS_LKG: kv }, NEW, await kv.getWithMetadata("k"), NOW_MS);
  assert.equal(wrote, true);
}
{
  const kv = mockKV(entry(NEW, "t"));
  const wrote = await putArtifact({ ...ENV_BASE, WEIGHTS_LKG: kv }, OLD, await kv.getWithMetadata("lkg:weights_next"), NOW_MS);
  assert.equal(wrote, false);
  assert.equal(kv.puts.length, 0);
}
{
  const kv = mockKV(entry(EXPIRED, "t"));
  const wrote = await putArtifact({ ...ENV_BASE, WEIGHTS_LKG: kv }, OLD, await kv.getWithMetadata("lkg:weights_next"), NOW_MS);
  assert.equal(wrote, true);
}

// ---- T1: fresh artifact served as "artifact"; background advances to newer ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, NEW);
  try {
    const kv = mockKV(entry(OLD, "2026-06-28T19:05:00.000Z"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, c);
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "artifact");
    assert.equal(await resp.text(), OLD);
    const cc = resp.headers.get("cache-control");
    assert.match(cc, /s-maxage=15/);
    await Promise.all(c.pending);
    assert.equal((await kv.getWithMetadata("lkg:weights_next")).value, NEW);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T2: fresh artifact NEW, origin OLDER -> NO regression ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, OLD);
  try {
    const kv = mockKV(entry(NEW, "2026-06-28T19:05:00.000Z"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, c);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "artifact");
    assert.equal(await resp.text(), NEW);
    await Promise.all(c.pending);
    assert.equal(kv.puts.length, 0);
    assert.equal((await kv.getWithMetadata("lkg:weights_next")).value, NEW);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T3: fresh artifact, origin SAME gen -> no overwrite ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, NEW);
  try {
    const kv = mockKV(entry(NEW, "t"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    await handleRequest(new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, c);
    await Promise.all(c.pending);
    assert.equal(kv.puts.length, 0);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T4: empty artifact, origin fresh -> first-fill, served as "origin" ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, NEW);
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, c);
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "origin");
    assert.equal(await resp.text(), NEW);
    assert.equal((await kv.getWithMetadata("lkg:weights_next")).value, NEW);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T5: expired artifact, origin fresh -> replaced, served as "origin" ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, NEW);
  try {
    const kv = mockKV(entry(EXPIRED, "t"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, ctx());
    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "origin");
    assert.equal((await kv.getWithMetadata("lkg:weights_next")).value, NEW);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T6: expired artifact, origin expired -> 503 weights_expired ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, EXPIRED);
  try {
    const kv = mockKV(entry(EXPIRED, "t"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, ctx());
    assert.equal(resp.status, 503);
    assert.equal(resp.headers.get("retry-after"), "2");
    const body = await resp.json();
    assert.equal(body.error, "weights_expired");
    assert.equal(body.reason, "origin_not_fresh");
    assert.equal(kv.puts.length, 0);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T7: empty artifact, origin 5xx -> 503 no vector available ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, "boom", 500);
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, ctx());
    assert.equal(resp.status, 503);
    const body = await resp.json();
    assert.equal(body.error, "no vector available");
    assert.equal(body.reason, "origin_status_500");
  } finally { globalThis.fetch = realFetch; }
}

// ---- T8: empty artifact, origin throws -> 503 origin_error ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("connect timeout"); };
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, ctx());
    assert.equal(resp.status, 503);
    const body = await resp.json();
    assert.equal(body.reason, "origin_error");
  } finally { globalThis.fetch = realFetch; }
}

// ---- T9: malformed origin, empty artifact -> 503, nothing stored ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, MALFORMED);
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, ctx());
    assert.equal(resp.status, 503);
    assert.equal(kv.puts.length, 0);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T10: fresh artifact, origin malformed -> serve artifact; no advance ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, MALFORMED);
  try {
    const kv = mockKV(entry(NEW, "t"));
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    const c = ctx();
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/validator/weights/next"), env, c);
    assert.equal(resp.headers.get("x-cathedral-vector-source"), "artifact");
    assert.equal(await resp.text(), NEW);
    await Promise.all(c.pending);
    assert.equal(kv.puts.length, 0);
  } finally { globalThis.fetch = realFetch; }
}

// ---- T11: legacy /api/cathedral prefix stripped for origin fetch ----
{
  const realFetch = globalThis.fetch;
  let seenUrl = null;
  globalThis.fetch = async (url) => { seenUrl = url; return new Response(NEW, { status: 200 }); };
  try {
    const kv = mockKV();
    const env = { ...ENV_BASE, WEIGHTS_LKG: kv };
    await handleRequest(
      new Request("https://api.cathedral.computer/api/cathedral/v1/validator/weights/next?x=1"),
      env, ctx());
    assert.equal(seenUrl, "https://publisher.example/v1/validator/weights/next?x=1");
  } finally { globalThis.fetch = realFetch; }
}

// ---- maybeAdvance directly: newer origin advances a fresh artifact ----
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = withFetch(null, NEW);
  try {
    const kv = mockKV(entry(OLD, "t"));
    await maybeAdvance("https://publisher.example/v1/validator/weights/next",
      { ...ENV_BASE, WEIGHTS_LKG: kv });
    assert.equal((await kv.getWithMetadata("lkg:weights_next")).value, NEW);
  } finally { globalThis.fetch = realFetch; }
}

// ---- serveStale: fresh stored served; expired stored -> 503 ----
{
  const kv = mockKV(entry(NEW, "2026-06-28T19:05:00.000Z"));
  const resp = await serveStale({ ...ENV_BASE, WEIGHTS_LKG: kv }, "manual");
  assert.equal(resp.status, 200);
  assert.equal(await resp.text(), NEW);
}
{
  const kv = mockKV(entry(EXPIRED, "t"));
  const resp = await serveStale({ ...ENV_BASE, WEIGHTS_LKG: kv }, "manual");
  assert.equal(resp.status, 503);
  assert.equal((await resp.json()).error, "weights_expired");
}

console.log("weights-failover artifact-first tests passed");
