// Run with: node checks.test.mjs   (same convention as deploy/edge-router)
import assert from "node:assert/strict";

import {
  DEFAULT_CONFIG,
  ageSeconds,
  checkV2Verify,
  checkValidatorHealth,
  checkWeightsFeed,
  configFromEnv,
  formatEventLines,
  formatWebhookPayload,
  reconcileAlerts,
} from "./checks.mjs";

const cfg = DEFAULT_CONFIG;
const NOW = Date.parse("2026-07-05T12:00:00.000Z");
const iso = (secsAgo) => new Date(NOW - secsAgo * 1000).toISOString();
const URL0 = "https://api.cathedral.computer/v1/validator/weights/next";

function ids(findings) {
  return findings.map((f) => `${f.level}:${f.id.split(":")[0]}`).sort();
}

// ---- config ------------------------------------------------------------------

assert.equal(configFromEnv({}).vectorPageSecs, 600);
assert.equal(configFromEnv({ VECTOR_PAGE_SECS: "900" }).vectorPageSecs, 900);
assert.equal(configFromEnv({ VECTOR_PAGE_SECS: "junk" }).vectorPageSecs, 600);
assert.equal(configFromEnv({ V2_EXPECTED: "0" }).v2Expected, false);
assert.equal(configFromEnv({ V2_EXPECTED: "true" }).v2Expected, true);

// ---- ageSeconds ----------------------------------------------------------------

assert.equal(ageSeconds(null, NOW), null);
assert.equal(ageSeconds("not-a-date", NOW), null);
assert.equal(Math.round(ageSeconds(iso(90), NOW)), 90);
// future timestamps clamp to 0, never negative
assert.equal(ageSeconds(iso(-30), NOW), 0);

// ---- weights feed ---------------------------------------------------------------

// healthy: 200, fresh vector, no findings
{
  const res = { status: 200, json: { generated_at: iso(60) }, sourceHeader: null, error: null };
  const { findings, failures } = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(findings, []);
  assert.equal(failures, 0);
}

// any 5xx pages immediately (ALERTS.md Tier 0)
{
  const res = { status: 503, json: null, sourceHeader: null, error: null };
  const { findings } = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(ids(findings), ["page:weights_5xx"]);
}

// stale_fallback via body source pages even when the vector is fresh
{
  const res = {
    status: 200,
    json: { generated_at: iso(30), source: "stale_fallback" },
    sourceHeader: null,
    error: null,
  };
  const { findings } = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(ids(findings), ["page:weights_stale_fallback"]);
}

// stale_fallback via the x-cathedral-vector-source header also pages
{
  const res = { status: 200, json: { generated_at: iso(30) }, sourceHeader: "stale_fallback", error: null };
  const { findings } = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(ids(findings), ["page:weights_stale_fallback"]);
}

// vector age: warn between warn and page thresholds, page above page threshold
{
  const warn = checkWeightsFeed(URL0, { status: 200, json: { generated_at: iso(400) }, sourceHeader: null, error: null }, NOW, 0, cfg);
  assert.deepEqual(ids(warn.findings), ["warn:weights_vector_age"]);
  const page = checkWeightsFeed(URL0, { status: 200, json: { generated_at: iso(700) }, sourceHeader: null, error: null }, NOW, 0, cfg);
  assert.deepEqual(ids(page.findings), ["page:weights_vector_age"]);
}

// missing generated_at is unknown, not silently healthy
{
  const res = { status: 200, json: {}, sourceHeader: null, error: null };
  const { findings } = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(ids(findings), ["warn:weights_vector_age"]);
}

// network failure: first blip is silent, second consecutive pages
{
  const res = { status: null, json: null, sourceHeader: null, error: "timeout" };
  const first = checkWeightsFeed(URL0, res, NOW, 0, cfg);
  assert.deepEqual(first.findings, []);
  assert.equal(first.failures, 1);
  const second = checkWeightsFeed(URL0, res, NOW, first.failures, cfg);
  assert.deepEqual(ids(second.findings), ["page:weights_unreachable"]);
  // recovery resets the counter
  const ok = checkWeightsFeed(URL0, { status: 200, json: { generated_at: iso(10) }, sourceHeader: null, error: null }, NOW, 2, cfg);
  assert.equal(ok.failures, 0);
}

// legacy-prefixed URL gets a distinct label so per-URL alerts do not collide
{
  const legacy = "https://api.cathedral.computer/api/cathedral/v1/validator/weights/next";
  const res = { status: 503, json: null, sourceHeader: null, error: null };
  const a = checkWeightsFeed(URL0, res, NOW, 0, cfg).findings[0];
  const b = checkWeightsFeed(legacy, res, NOW, 0, cfg).findings[0];
  assert.notEqual(a.id, b.id);
}

// ---- validator-health -------------------------------------------------------------

function healthBody(overrides = {}) {
  return {
    schema: "cathedral.validator_health.v1",
    weights_feed: {
      freshness: { level: "ok", age_seconds: 45 },
      feed_5xx: 0,
      ...(overrides.weights_feed || {}),
    },
    http_status: {
      started_at_iso: "2026-07-05T00:00:00.000Z",
      total: 1000,
      by_class: { "2xx": 990, "5xx": 0 },
      ...(overrides.http_status || {}),
    },
    submit: { by_reason: {}, ...(overrides.submit || {}) },
    ratelimit: overrides.ratelimit,
  };
}

// healthy body, no baseline: no findings, baseline captured
{
  const { findings, next } = checkValidatorHealth(
    { status: 200, json: healthBody(), error: null }, null, NOW, cfg);
  assert.deepEqual(findings, []);
  assert.equal(next.startedAt, "2026-07-05T00:00:00.000Z");
  assert.equal(next.unresolvedIp, null);
}

// origin-side freshness levels map through
{
  const page = checkValidatorHealth(
    { status: 200, json: healthBody({ weights_feed: { freshness: { level: "page", age_seconds: 700 } } }), error: null },
    null, NOW, cfg);
  assert.deepEqual(ids(page.findings), ["page:origin_vector_freshness"]);
  const unknown = checkValidatorHealth(
    { status: 200, json: healthBody({ weights_feed: { freshness: { level: "unknown" } } }), error: null },
    null, NOW, cfg);
  assert.deepEqual(ids(unknown.findings), ["warn:origin_vector_freshness"]);
}

// counted feed 5xx delta pages; 429 rate warns; both need a same-process baseline
{
  const prev = {
    atMs: NOW - 60000,
    startedAt: "2026-07-05T00:00:00.000Z",
    feed5xx: 0,
    total: 1000,
    total5xx: 0,
    busy429: 0,
    unresolvedIp: null,
  };
  const body = healthBody({
    weights_feed: { feed_5xx: 2 },
    http_status: { total: 2000, by_class: { "5xx": 1 } },
    submit: { by_reason: { submit_busy_retry: 100, per_miner_busy_retry: 100 } },
  });
  const { findings } = checkValidatorHealth({ status: 200, json: body, error: null }, prev, NOW, cfg);
  assert.deepEqual(ids(findings), ["page:weights_feed_5xx_counted", "warn:submit_429_rate"]);
}

// restart (different started_at_iso) suppresses deltas instead of false-firing
{
  const prev = {
    atMs: NOW - 60000, startedAt: "2026-07-04T00:00:00.000Z",
    feed5xx: 50, total: 99999, total5xx: 500, busy429: 99999, unresolvedIp: 10,
  };
  const { findings, next } = checkValidatorHealth(
    { status: 200, json: healthBody(), error: null }, prev, NOW, cfg);
  assert.deepEqual(findings, []);
  assert.equal(next.startedAt, "2026-07-05T00:00:00.000Z");
}

// 5xx rate: needs the minimum sample
{
  const prev = {
    atMs: NOW - 60000, startedAt: "2026-07-05T00:00:00.000Z",
    feed5xx: 0, total: 1000, total5xx: 0, busy429: 0, unresolvedIp: null,
  };
  const small = healthBody({ http_status: { total: 1005, by_class: { "5xx": 2 } } });
  assert.deepEqual(
    checkValidatorHealth({ status: 200, json: small, error: null }, prev, NOW, cfg).findings, []);
  const big = healthBody({ http_status: { total: 1100, by_class: { "5xx": 10 } } });
  assert.deepEqual(
    ids(checkValidatorHealth({ status: 200, json: big, error: null }, prev, NOW, cfg).findings),
    ["warn:http_5xx_rate"]);
}

// unresolved_ip_count: rising warns; null (not yet wired on the deployed rev) is quiet
{
  const prev = {
    atMs: NOW - 60000, startedAt: "2026-07-05T00:00:00.000Z",
    feed5xx: 0, total: 1000, total5xx: 0, busy429: 0, unresolvedIp: 3,
  };
  const rising = healthBody({ ratelimit: { unresolved_ip_count: 8 } });
  assert.deepEqual(
    ids(checkValidatorHealth({ status: 200, json: rising, error: null }, prev, NOW, cfg).findings),
    ["warn:ratelimit_fail_open"]);
  const absent = healthBody();
  assert.deepEqual(
    checkValidatorHealth({ status: 200, json: absent, error: null }, { ...prev, unresolvedIp: null }, NOW, cfg).findings,
    []);
}

// auth and transport failures surface as warns and keep the old baseline
{
  const prev = { atMs: NOW - 60000, startedAt: "x", feed5xx: 1, total: 1, total5xx: 0, busy429: 0, unresolvedIp: null };
  const auth = checkValidatorHealth({ status: 401, json: null, error: null }, prev, NOW, cfg);
  assert.deepEqual(ids(auth.findings), ["warn:validator_health_auth"]);
  assert.equal(auth.next, prev);
  const down = checkValidatorHealth({ status: null, json: null, error: "timeout" }, prev, NOW, cfg);
  assert.deepEqual(ids(down.findings), ["warn:validator_health_unreachable"]);
  assert.equal(down.next, prev);
}

// ---- v2 verify metrics ---------------------------------------------------------

function v2Body(overrides = {}) {
  return {
    schema: "cathedral.v2.verify_metrics.v1",
    enabled: true,
    pending_count: 0,
    processed_last_60s: 0,
    oldest_pending_age_secs: null,
    tick_errors_last_60s: 0,
    ...overrides,
  };
}

// healthy: empty queue, nothing fires (idle worker is not "stalled")
{
  const { findings, next } = checkV2Verify({ status: 200, json: v2Body(), error: null }, null, NOW, cfg);
  assert.deepEqual(findings, []);
  assert.equal(next.failures, 0);
  assert.equal(next.history.length, 1);
}

// backlog depth thresholds
{
  const warn = checkV2Verify({ status: 200, json: v2Body({ pending_count: 6000, processed_last_60s: 900 }), error: null }, null, NOW, cfg);
  assert.deepEqual(ids(warn.findings), ["warn:v2_backlog_depth"]);
  const page = checkV2Verify({ status: 200, json: v2Body({ pending_count: 25000, processed_last_60s: 900 }), error: null }, null, NOW, cfg);
  assert.deepEqual(ids(page.findings), ["page:v2_backlog_depth"]);
}

// rising trend across N consecutive polls warns once above the floor
{
  let state = null;
  const series = [600, 700, 800, 900, 1000, 1100];
  let last;
  for (const pending of series) {
    last = checkV2Verify(
      { status: 200, json: v2Body({ pending_count: pending, processed_last_60s: 500 }), error: null },
      state, NOW, cfg);
    state = last.next;
  }
  assert.deepEqual(ids(last.findings), ["warn:v2_backlog_trend"]);
}

// same shape below the floor stays quiet
{
  let state = null;
  let last;
  for (const pending of [3, 5, 8, 13, 21, 34]) {
    last = checkV2Verify(
      { status: 200, json: v2Body({ pending_count: pending, processed_last_60s: 500 }), error: null },
      state, NOW, cfg);
    state = last.next;
  }
  assert.deepEqual(last.findings, []);
}

// a non-monotonic series stays quiet
{
  let state = null;
  let last;
  for (const pending of [600, 700, 650, 900, 1000, 1100]) {
    last = checkV2Verify(
      { status: 200, json: v2Body({ pending_count: pending, processed_last_60s: 500 }), error: null },
      state, NOW, cfg);
    state = last.next;
  }
  assert.deepEqual(last.findings, []);
}

// oldest pending age: warn then page (SLO 99% in 5m)
{
  const warn = checkV2Verify({ status: 200, json: v2Body({ pending_count: 5, oldest_pending_age_secs: 400, processed_last_60s: 10 }), error: null }, null, NOW, cfg);
  assert.deepEqual(ids(warn.findings), ["warn:v2_oldest_pending"]);
  const page = checkV2Verify({ status: 200, json: v2Body({ pending_count: 5, oldest_pending_age_secs: 1000, processed_last_60s: 10 }), error: null }, null, NOW, cfg);
  assert.deepEqual(ids(page.findings), ["page:v2_oldest_pending"]);
}

// wedged worker: pending stays, drain is zero for two polls
{
  const res = { status: 200, json: v2Body({ pending_count: 50, processed_last_60s: 0 }), error: null };
  const first = checkV2Verify(res, null, NOW, cfg);
  assert.equal(first.findings.some((f) => f.id === "v2_worker_stalled"), false);
  const second = checkV2Verify(res, first.next, NOW, cfg);
  assert.equal(second.findings.some((f) => f.id === "v2_worker_stalled" && f.level === "page"), true);
  // draining again clears it
  const third = checkV2Verify(
    { status: 200, json: v2Body({ pending_count: 40, processed_last_60s: 300 }), error: null },
    second.next, NOW, cfg);
  assert.equal(third.findings.some((f) => f.id === "v2_worker_stalled"), false);
}

// worker disabled + tick errors warn
{
  const { findings } = checkV2Verify(
    { status: 200, json: v2Body({ enabled: false, tick_errors_last_60s: 3, last_worker_error: "boom" }), error: null },
    null, NOW, cfg);
  assert.deepEqual(ids(findings), ["warn:v2_tick_errors", "warn:v2_worker_disabled"]);
  assert.ok(findings.find((f) => f.id === "v2_tick_errors").title.includes("boom"));
}

// unreachable v2 origin: warn first, page on the second consecutive failure
{
  const res = { status: null, json: null, error: "connect timeout" };
  const first = checkV2Verify(res, null, NOW, cfg);
  assert.deepEqual(ids(first.findings), ["warn:v2_metrics_unreachable"]);
  const second = checkV2Verify(res, first.next, NOW, cfg);
  assert.deepEqual(ids(second.findings), ["page:v2_metrics_unreachable"]);
}

// 404: warn while the ramp expects v2; quiet when V2_EXPECTED=0
{
  const res = { status: 404, json: null, error: null };
  assert.deepEqual(ids(checkV2Verify(res, null, NOW, cfg).findings), ["warn:v2_metrics_missing"]);
  const quiet = checkV2Verify(res, null, NOW, { ...cfg, v2Expected: false });
  assert.deepEqual(quiet.findings, []);
}

// ---- alert lifecycle ---------------------------------------------------------------

{
  const f = (id, level) => ({ id, level, title: `t:${id}` });
  const realertMs = 30 * 60000;

  // fire on first sight
  let { alerts, events } = reconcileAlerts([f("a", "warn")], {}, NOW, realertMs);
  assert.deepEqual(events.map((e) => e.type), ["fire"]);

  // still active shortly after: silent
  ({ alerts, events } = reconcileAlerts([f("a", "warn")], alerts, NOW + 60000, realertMs));
  assert.deepEqual(events, []);

  // escalation warn -> page sends immediately
  ({ alerts, events } = reconcileAlerts([f("a", "page")], alerts, NOW + 120000, realertMs));
  assert.deepEqual(events.map((e) => e.type), ["escalate"]);
  assert.equal(alerts.a.sinceMs, NOW);

  // re-alert after the window
  ({ alerts, events } = reconcileAlerts([f("a", "page")], alerts, NOW + 120000 + realertMs, realertMs));
  assert.deepEqual(events.map((e) => e.type), ["realert"]);

  // recovery sends a resolve and drops the alert
  ({ alerts, events } = reconcileAlerts([], alerts, NOW + 200000 + realertMs, realertMs));
  assert.deepEqual(events.map((e) => e.type), ["resolve"]);
  assert.deepEqual(alerts, {});
}

// ---- formatting ---------------------------------------------------------------------

{
  const events = [
    { type: "resolve", id: "c", level: "warn", title: "warn thing cleared", sinceMs: NOW - 45 * 60000 },
    { type: "fire", id: "a", level: "warn", title: "warn thing" },
    { type: "fire", id: "b", level: "page", title: "page thing" },
    { type: "realert", id: "d", level: "page", title: "old page", sinceMs: NOW - 90 * 60000 },
  ];
  const lines = formatEventLines(events, NOW);
  assert.equal(lines[0], "[PAGE] page thing");
  assert.equal(lines[1], "[WARN] warn thing");
  assert.equal(lines[2], "[PAGE 1h30m] old page");
  assert.equal(lines[3], "[RESOLVED] warn thing cleared (was warn, active 45m)");

  const discord = formatWebhookPayload(events, "discord", NOW);
  assert.ok(discord.content.startsWith("cathedral watcher\n[PAGE] page thing"));
  const slack = formatWebhookPayload(events, "slack", NOW);
  assert.ok(slack.text.includes("[PAGE] page thing"));
  const json = formatWebhookPayload(events, "json", NOW);
  assert.equal(json.events.length, 4);

  // discord 2000-char content cap holds
  const many = Array.from({ length: 100 }, (_, i) => ({
    type: "fire", id: `x${i}`, level: "warn", title: "y".repeat(80),
  }));
  const big = formatWebhookPayload(many, "discord", NOW);
  assert.ok(big.content.length <= 2000);
  assert.ok(big.content.endsWith("...(truncated)"));
}

console.log("checks.test.mjs: all assertions passed");
