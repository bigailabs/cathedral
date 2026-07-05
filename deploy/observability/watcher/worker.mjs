// cathedral-alert-watcher: cron-triggered Cloudflare Worker that polls the
// ramp-critical health surfaces and pushes alerts to a webhook.
//
// Runs OFF-BOX on purpose: an in-process watcher dies with the origin it is
// watching. Both recent incidents (the pm-read 429 bug and the submit outage)
// were reported by miners before any internal signal; this worker exists so
// the webhook fires first.
//
// All threshold and alert-lifecycle logic lives in checks.mjs (pure, tested).
// This file only does I/O: fetch the surfaces, load/save KV state, deliver.

import {
  checkV2Verify,
  checkValidatorHealth,
  checkWeightsFeed,
  configFromEnv,
  formatWebhookPayload,
  numberFromEnv,
  reconcileAlerts,
  rollbackUndelivered,
} from "./checks.mjs";

const STATE_KEY = "state";

const DEFAULT_WEIGHTS_URLS = [
  "https://api.cathedral.computer/v1/validator/weights/next",
  "https://api.cathedral.computer/api/cathedral/v1/validator/weights/next",
  "https://read.cathedral.computer/v1/validator/weights/next",
].join(",");

function weightsUrls(env) {
  return String(env.WEIGHTS_URLS || DEFAULT_WEIGHTS_URLS)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function fetchSurface(url, { headers = {}, timeoutMs = 10000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort("timeout"), timeoutMs);
  try {
    const resp = await fetch(url, {
      headers: { "cache-control": "no-cache", ...headers },
      signal: controller.signal,
      redirect: "manual",
    });
    let json = null;
    try {
      json = await resp.json();
    } catch {
      json = null;
    }
    return {
      status: resp.status,
      json,
      sourceHeader: resp.headers.get("x-cathedral-vector-source"),
      error: null,
    };
  } catch (err) {
    return { status: null, json: null, sourceHeader: null, error: String(err).slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
}

async function loadState(env) {
  if (!env.ALERT_STATE) return { state: {}, stateless: true };
  try {
    const state = (await env.ALERT_STATE.get(STATE_KEY, "json")) || {};
    return { state, stateless: false };
  } catch (err) {
    console.log(`alert-watcher: KV read failed: ${err}`);
    return { state: {}, stateless: true };
  }
}

async function saveState(env, state) {
  if (!env.ALERT_STATE) return;
  try {
    await env.ALERT_STATE.put(STATE_KEY, JSON.stringify(state));
  } catch (err) {
    console.log(`alert-watcher: KV write failed: ${err}`);
  }
}

async function deliver(env, events, nowMs, notes) {
  const format = String(env.WEBHOOK_FORMAT || "discord").toLowerCase();
  const payload = formatWebhookPayload(events, format, nowMs);
  if (notes.length && payload.content) payload.content += `\n(${notes.join("; ")})`;
  if (notes.length && payload.text) payload.text += `\n(${notes.join("; ")})`;

  if (!env.ALERT_WEBHOOK_URL) {
    console.log(`alert-watcher: ALERT_WEBHOOK_URL not set; undelivered events: ${JSON.stringify(events)}`);
    return false;
  }
  // Bound the webhook fetch the same way surface fetches are bounded — a hung
  // Discord must not stall the run (which would also skip persisting counter
  // baselines). Any failure (non-2xx, timeout, network) returns false so the
  // caller can roll back state and retry next poll instead of losing the page.
  const controller = new AbortController();
  const timeoutMs = numberFromEnv(env, "WEBHOOK_TIMEOUT_MS", 8000);
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(env.ALERT_WEBHOOK_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!resp.ok) {
      console.log(`alert-watcher: webhook delivery failed HTTP ${resp.status}`);
      return false;
    }
    return true;
  } catch (err) {
    console.log(`alert-watcher: webhook delivery threw: ${err && err.name ? err.name : err}`);
    return false;
  } finally {
    clearTimeout(t);
  }
}

export async function runOnce(env, { send = true, persist = true } = {}) {
  const nowMs = Date.now();
  const cfg = configFromEnv(env);
  const timeoutMs = numberFromEnv(env, "FETCH_TIMEOUT_MS", 10000);
  const { state, stateless } = await loadState(env);
  const notes = [];
  if (stateless) {
    notes.push("stateless run: ALERT_STATE KV not bound; dedup/trend disabled");
  }

  const urls = weightsUrls(env);
  const publisherOrigin = String(env.PUBLISHER_ORIGIN || "https://api.cathedral.computer").replace(/\/+$/, "");
  const v2Origin = String(env.V2_ORIGIN || "https://v2-beta.cathedral.computer").replace(/\/+$/, "");
  const adminToken = env.CATHEDRAL_ADMIN_TOKEN || null;

  const fetches = [
    ...urls.map((u) => fetchSurface(u, { timeoutMs })),
    adminToken
      ? fetchSurface(`${publisherOrigin}/v1/admin/validator-health`, {
          timeoutMs,
          headers: { authorization: `Bearer ${adminToken}` },
        })
      : Promise.resolve(null),
    fetchSurface(`${v2Origin}/v2/verify/metrics`, { timeoutMs }),
  ];
  const results = await Promise.all(fetches);
  const weightsResults = results.slice(0, urls.length);
  const healthResult = results[urls.length];
  const v2Result = results[urls.length + 1];

  const findings = [];
  const weightsFailures = {};
  urls.forEach((url, i) => {
    const prevFails = state.weightsFailures?.[url] || 0;
    const { findings: f, failures } = checkWeightsFeed(url, weightsResults[i], nowMs, prevFails, cfg);
    findings.push(...f);
    weightsFailures[url] = failures;
  });

  let healthBaseline = state.health || null;
  if (healthResult) {
    const { findings: f, next } = checkValidatorHealth(healthResult, state.health || null, nowMs, cfg);
    findings.push(...f);
    healthBaseline = next;
  } else if (!adminToken) {
    notes.push("validator-health skipped: CATHEDRAL_ADMIN_TOKEN secret not set");
  }

  const { findings: v2Findings, next: v2State } = checkV2Verify(v2Result, state.v2 || null, nowMs, cfg);
  findings.push(...v2Findings);

  const { alerts, events } = reconcileAlerts(
    findings, state.alerts || {}, nowMs, cfg.realertMins * 60000);

  let delivered = false;
  if (send && events.length) {
    delivered = await deliver(env, events, nowMs, notes);
  }

  // If there were events to send but delivery failed, rewind the alert state so
  // the undelivered events (including a first PAGE) re-fire next poll instead of
  // being swallowed or delayed by the realert window.
  let alertsToPersist = alerts;
  if (send && events.length && !delivered) {
    alertsToPersist = rollbackUndelivered(alerts, state.alerts || {}, events);
  }

  if (persist) {
    await saveState(env, {
      atMs: nowMs,
      alerts: alertsToPersist,
      health: healthBaseline,
      v2: v2State,
      weightsFailures,
    });
  }

  return { nowMs, findings, events, alerts, notes, delivered };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runOnce(env, { send: true, persist: true }));
  },

  // Read-only status pane for manual checks: runs the same probes, sends
  // nothing, persists nothing. Gate with the STATUS_TOKEN secret if the
  // workers.dev URL should not expose ops telemetry.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/status") {
      return new Response("not found", { status: 404 });
    }
    if (env.STATUS_TOKEN) {
      const auth = request.headers.get("authorization") || "";
      if (auth !== `Bearer ${env.STATUS_TOKEN}`) {
        return new Response("unauthorized", { status: 401 });
      }
    }
    const result = await runOnce(env, { send: false, persist: false });
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  },
};
