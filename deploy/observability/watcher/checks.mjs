// Pure threshold + alert-state logic for the cathedral alert watcher.
// No fetch, no env, no KV in this module: everything here takes plain values
// and returns plain values so it can be unit-tested with `node checks.test.mjs`.
//
// Thresholds mirror deploy/observability/ALERTS.md and
// scaffold/publisher/health_thresholds.py. If a number here disagrees with
// ALERTS.md, ALERTS.md wins and this file is the bug.

export const DEFAULT_CONFIG = {
  // Signed-vector freshness (now - generated_at), seconds.
  // health_thresholds.py: warn > 300, page > 600.
  vectorWarnSecs: 300,
  vectorPageSecs: 600,

  // v2 verify backlog depth (pending_count). The single verify worker drains
  // ~2000/min, so 20k pending is ~10 min of pure drain with zero intake.
  pendingWarn: 5000,
  pendingPage: 20000,

  // Backlog trend: warn when pending rises across this many consecutive polls
  // and is above the floor (the floor keeps a 3 -> 5 -> 8 trickle quiet).
  pendingTrendPolls: 5,
  pendingTrendFloor: 500,

  // Oldest pending submit age, seconds. SLO: 95% verified in 30s, 99% in 5m.
  oldestPendingWarnSecs: 300,
  oldestPendingPageSecs: 900,

  // Worker considered stalled when pending stays >= this across two polls
  // while processed_last_60s is 0 both times.
  stallMinPending: 10,

  // submit_busy_retry + per_miner_busy_retry 429s per minute (counter delta).
  busy429WarnPerMin: 120,

  // Global 5xx rate over the polling window (delta-based), with a minimum
  // request-count sample so 1 error out of 2 requests does not fire.
  rate5xxWarn: 0.02,
  rate5xxMinSample: 20,

  // Network-level fetch failures must repeat this many consecutive polls
  // before paging (one blip from one PoP should not wake anyone).
  netFailPagePolls: 2,

  // Re-send an alert that is still active after this many minutes.
  realertMins: 30,

  // The v2 fast path is being ramped, so a 404 from /v2/verify/metrics is a
  // finding, not an expected state. Set V2_EXPECTED=0 to silence.
  v2Expected: true,
};

export function numberFromEnv(env, key, fallback) {
  const raw = env && env[key];
  if (raw === undefined || raw === null || String(raw).trim() === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

export function boolFromEnv(env, key, fallback) {
  const raw = env && env[key];
  if (raw === undefined || raw === null || String(raw).trim() === "") return fallback;
  return !["0", "false", "no", "off"].includes(String(raw).trim().toLowerCase());
}

export function configFromEnv(env) {
  const d = DEFAULT_CONFIG;
  return {
    vectorWarnSecs: numberFromEnv(env, "VECTOR_WARN_SECS", d.vectorWarnSecs),
    vectorPageSecs: numberFromEnv(env, "VECTOR_PAGE_SECS", d.vectorPageSecs),
    pendingWarn: numberFromEnv(env, "PENDING_WARN", d.pendingWarn),
    pendingPage: numberFromEnv(env, "PENDING_PAGE", d.pendingPage),
    pendingTrendPolls: numberFromEnv(env, "PENDING_TREND_POLLS", d.pendingTrendPolls),
    pendingTrendFloor: numberFromEnv(env, "PENDING_TREND_FLOOR", d.pendingTrendFloor),
    oldestPendingWarnSecs: numberFromEnv(env, "OLDEST_PENDING_WARN_SECS", d.oldestPendingWarnSecs),
    oldestPendingPageSecs: numberFromEnv(env, "OLDEST_PENDING_PAGE_SECS", d.oldestPendingPageSecs),
    stallMinPending: numberFromEnv(env, "STALL_MIN_PENDING", d.stallMinPending),
    busy429WarnPerMin: numberFromEnv(env, "BUSY_429_WARN_PER_MIN", d.busy429WarnPerMin),
    rate5xxWarn: numberFromEnv(env, "RATE_5XX_WARN", d.rate5xxWarn),
    rate5xxMinSample: numberFromEnv(env, "RATE_5XX_MIN_SAMPLE", d.rate5xxMinSample),
    netFailPagePolls: numberFromEnv(env, "NET_FAIL_PAGE_POLLS", d.netFailPagePolls),
    realertMins: numberFromEnv(env, "REALERT_MINS", d.realertMins),
    v2Expected: boolFromEnv(env, "V2_EXPECTED", d.v2Expected),
  };
}

function hostLabel(url) {
  try {
    const u = new URL(url);
    // Distinguish the canonical and legacy-prefixed routes on the same host.
    return u.pathname.startsWith("/api/cathedral") ? `${u.host}(legacy)` : u.host;
  } catch {
    return String(url);
  }
}

export function ageSeconds(iso, nowMs) {
  if (typeof iso !== "string" || !iso) return null;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  return Math.max(0, (nowMs - t) / 1000);
}

// ---- Tier 0: validator weight feed -----------------------------------------
// res: { status: number|null, json: object|null, sourceHeader: string|null,
//        error: string|null }
// consecutiveFailures: prior consecutive network-failure count for this URL.
// Returns { findings, failures }.
export function checkWeightsFeed(url, res, nowMs, consecutiveFailures, cfg) {
  const label = hostLabel(url);
  const findings = [];
  const prevFails = Number(consecutiveFailures) || 0;

  if (!res || res.error != null || res.status == null) {
    const failures = prevFails + 1;
    if (failures >= cfg.netFailPagePolls) {
      findings.push({
        id: `weights_unreachable:${label}`,
        level: "page",
        title: `weights feed unreachable ${failures} polls: ${url} (${res && res.error ? res.error : "no response"})`,
      });
    }
    return { findings, failures };
  }

  if (res.status >= 500) {
    // ALERTS.md Tier 0: any 5xx on the weight feed is a PAGE, no debounce.
    findings.push({
      id: `weights_5xx:${label}`,
      level: "page",
      title: `weights feed HTTP ${res.status}: ${url}`,
    });
    return { findings, failures: 0 };
  }
  if (res.status !== 200) {
    findings.push({
      id: `weights_bad_status:${label}`,
      level: "page",
      title: `weights feed HTTP ${res.status} (expected 200): ${url}`,
    });
    return { findings, failures: 0 };
  }

  const body = res.json || {};
  const source = body.source ?? null;
  if (source === "stale_fallback" || res.sourceHeader === "stale_fallback") {
    // Page on the first stale serve: it means the origin is down, even while
    // the stale vector is still inside the acceptable age ceiling.
    findings.push({
      id: `weights_stale_fallback:${label}`,
      level: "page",
      title: `weights feed serving stale_fallback (origin down): ${url}`,
    });
  }

  const age = ageSeconds(body.generated_at, nowMs);
  if (age === null) {
    findings.push({
      id: `weights_vector_age:${label}`,
      level: "warn",
      title: `weights feed generated_at missing/unparseable: ${url}`,
    });
  } else if (age > cfg.vectorPageSecs) {
    findings.push({
      id: `weights_vector_age:${label}`,
      level: "page",
      title: `signed vector age ${Math.round(age)}s (> ${cfg.vectorPageSecs}s): ${url}`,
    });
  } else if (age > cfg.vectorWarnSecs) {
    findings.push({
      id: `weights_vector_age:${label}`,
      level: "warn",
      title: `signed vector age ${Math.round(age)}s (> ${cfg.vectorWarnSecs}s): ${url}`,
    });
  }

  return { findings, failures: 0 };
}

// ---- /v1/admin/validator-health ---------------------------------------------
// prev: { atMs, startedAt, feed5xx, total, total5xx, busy429, unresolvedIp } | null
// Returns { findings, next } where next is the counter baseline to persist.
export function checkValidatorHealth(res, prev, nowMs, cfg) {
  const findings = [];

  if (!res || res.error != null || res.status == null) {
    findings.push({
      id: "validator_health_unreachable",
      level: "warn",
      title: `validator-health unreachable: ${res && res.error ? res.error : "no response"}`,
    });
    return { findings, next: prev || null };
  }
  if (res.status === 401 || res.status === 403) {
    findings.push({
      id: "validator_health_auth",
      level: "warn",
      title: `validator-health rejected the admin token (HTTP ${res.status}); check CATHEDRAL_ADMIN_TOKEN`,
    });
    return { findings, next: prev || null };
  }
  if (res.status !== 200) {
    findings.push({
      id: "validator_health_error",
      level: "warn",
      title: `validator-health HTTP ${res.status}`,
    });
    return { findings, next: prev || null };
  }

  const body = res.json || {};
  const freshness = body.weights_feed?.freshness || {};
  const level = freshness.level;
  if (level === "page" || freshness.over_hard_ceiling === true) {
    findings.push({
      id: "origin_vector_freshness",
      level: "page",
      title: `origin reports vector freshness level=${level}` +
        (freshness.age_seconds != null ? ` age=${Math.round(freshness.age_seconds)}s` : "") +
        (freshness.over_hard_ceiling ? " (over hard ceiling)" : ""),
    });
  } else if (level === "warn") {
    findings.push({
      id: "origin_vector_freshness",
      level: "warn",
      title: `origin reports vector freshness level=warn` +
        (freshness.age_seconds != null ? ` age=${Math.round(freshness.age_seconds)}s` : ""),
    });
  } else if (level === "unknown") {
    findings.push({
      id: "origin_vector_freshness",
      level: "warn",
      title: "origin reports vector freshness level=unknown (no cached vector; cold process?)",
    });
  }

  const startedAt = body.http_status?.started_at_iso ?? null;
  const busyReasons = body.submit?.by_reason || {};
  const cur = {
    atMs: nowMs,
    startedAt,
    feed5xx: Number(body.weights_feed?.feed_5xx ?? 0),
    total: Number(body.http_status?.total ?? 0),
    total5xx: Number(body.http_status?.by_class?.["5xx"] ?? 0),
    busy429: Number(busyReasons.submit_busy_retry ?? 0) + Number(busyReasons.per_miner_busy_retry ?? 0),
    unresolvedIp: (typeof body.ratelimit?.unresolved_ip_count === "number")
      ? body.ratelimit.unresolved_ip_count : null,
  };

  // Counter deltas are only meaningful within one process lifetime.
  if (prev && prev.startedAt && prev.startedAt === startedAt) {
    const elapsedMin = (nowMs - Number(prev.atMs || 0)) / 60000;
    if (elapsedMin > 0) {
      const dFeed5xx = cur.feed5xx - Number(prev.feed5xx || 0);
      if (dFeed5xx > 0) {
        findings.push({
          id: "weights_feed_5xx_counted",
          level: "page",
          title: `origin counted ${dFeed5xx} weight-feed 5xx since last poll`,
        });
      }

      const dTotal = cur.total - Number(prev.total || 0);
      const d5xx = cur.total5xx - Number(prev.total5xx || 0);
      if (dTotal >= cfg.rate5xxMinSample && d5xx / dTotal > cfg.rate5xxWarn) {
        findings.push({
          id: "http_5xx_rate",
          level: "warn",
          title: `global 5xx rate ${(100 * d5xx / dTotal).toFixed(1)}% over last poll window (${d5xx}/${dTotal})`,
        });
      }

      const d429 = cur.busy429 - Number(prev.busy429 || 0);
      const perMin = d429 / elapsedMin;
      if (d429 >= 0 && perMin > cfg.busy429WarnPerMin) {
        findings.push({
          id: "submit_429_rate",
          level: "warn",
          title: `busy-retry 429 rate ${Math.round(perMin)}/min (submit+pm-read gates saturating)`,
        });
      }

      if (cur.unresolvedIp != null && prev.unresolvedIp != null &&
          cur.unresolvedIp > prev.unresolvedIp) {
        findings.push({
          id: "ratelimit_fail_open",
          level: "warn",
          title: `ratelimit unresolved_ip_count rose by ${cur.unresolvedIp - prev.unresolvedIp} (client-IP fail-open; limiter may be bypassable)`,
        });
      }
    }
  }

  return { findings, next: cur };
}

// ---- /v2/verify/metrics ------------------------------------------------------
// prev: { failures, history: [{ pending, processed }] } | null
// Returns { findings, next }.
export function checkV2Verify(res, prev, nowMs, cfg) {
  const findings = [];
  const prevFails = Number(prev?.failures) || 0;
  const history = Array.isArray(prev?.history) ? prev.history.slice() : [];

  if (!res || res.error != null || res.status == null) {
    const failures = prevFails + 1;
    findings.push({
      id: "v2_metrics_unreachable",
      level: failures >= cfg.netFailPagePolls ? "page" : "warn",
      title: `v2 origin unreachable ${failures} poll(s); fast-path miners may be unable to submit (${res && res.error ? res.error : "no response"})`,
    });
    return { findings, next: { failures, history } };
  }

  if (res.status === 404) {
    if (cfg.v2Expected) {
      findings.push({
        id: "v2_metrics_missing",
        level: "warn",
        title: "GET /v2/verify/metrics returned 404 (v2 flag off?) while the fast path is being ramped",
      });
    }
    return { findings, next: { failures: 0, history } };
  }

  if (res.status !== 200) {
    const failures = prevFails + 1;
    findings.push({
      id: "v2_metrics_error",
      level: failures >= cfg.netFailPagePolls ? "page" : "warn",
      title: `v2 verify metrics HTTP ${res.status} (${failures} consecutive)`,
    });
    return { findings, next: { failures, history } };
  }

  const body = res.json || {};
  const pending = Number(body.pending_count ?? 0);
  const processed = Number(body.processed_last_60s ?? 0);
  const oldestAge = (typeof body.oldest_pending_age_secs === "number")
    ? body.oldest_pending_age_secs : null;

  if (pending >= cfg.pendingPage) {
    findings.push({
      id: "v2_backlog_depth",
      level: "page",
      title: `v2 verify backlog ${pending} pending (>= ${cfg.pendingPage}); drain ceiling ~2000/min`,
    });
  } else if (pending >= cfg.pendingWarn) {
    findings.push({
      id: "v2_backlog_depth",
      level: "warn",
      title: `v2 verify backlog ${pending} pending (>= ${cfg.pendingWarn})`,
    });
  }

  history.push({ pending, processed });
  const keep = Math.max(cfg.pendingTrendPolls + 1, 3);
  while (history.length > keep) history.shift();

  if (history.length >= cfg.pendingTrendPolls + 1) {
    const window = history.slice(-(cfg.pendingTrendPolls + 1));
    let rising = true;
    for (let i = 1; i < window.length; i++) {
      if (window[i].pending <= window[i - 1].pending) { rising = false; break; }
    }
    if (rising && pending >= cfg.pendingTrendFloor) {
      findings.push({
        id: "v2_backlog_trend",
        level: "warn",
        title: `v2 verify backlog rising ${cfg.pendingTrendPolls} consecutive polls: ${window[0].pending} -> ${pending} (intake outpacing verify)`,
      });
    }
  }

  if (pending > 0 && oldestAge != null) {
    if (oldestAge >= cfg.oldestPendingPageSecs) {
      findings.push({
        id: "v2_oldest_pending",
        level: "page",
        title: `oldest pending v2 submit ${Math.round(oldestAge)}s old (SLO: 99% verified in 300s)`,
      });
    } else if (oldestAge >= cfg.oldestPendingWarnSecs) {
      findings.push({
        id: "v2_oldest_pending",
        level: "warn",
        title: `oldest pending v2 submit ${Math.round(oldestAge)}s old (SLO: 99% verified in 300s)`,
      });
    }
  }

  // Wedged worker: work exists but nothing drained for two consecutive polls.
  // This is deliberately backlog-based rather than last_batch_at-based, since
  // last_batch_at only updates on non-empty batches and goes stale while idle.
  const lastTwo = history.slice(-2);
  if (lastTwo.length === 2 &&
      lastTwo.every((h) => h.pending >= cfg.stallMinPending && h.processed === 0)) {
    findings.push({
      id: "v2_worker_stalled",
      level: "page",
      title: `v2 verify worker not draining: ${pending} pending, 0 processed across 2 polls (wedged worker or lost lock)`,
    });
  }

  if (body.enabled === false) {
    findings.push({
      id: "v2_worker_disabled",
      level: "warn",
      title: "v2 verify worker reports enabled=false (metrics on, worker off)",
    });
  }

  const tickErrors = Number(body.tick_errors_last_60s ?? 0);
  if (tickErrors > 0) {
    const lastErr = typeof body.last_worker_error === "string"
      ? ` last_error=${body.last_worker_error.slice(0, 120)}` : "";
    findings.push({
      id: "v2_tick_errors",
      level: "warn",
      title: `v2 verify worker ${tickErrors} tick error(s) in last 60s${lastErr}`,
    });
  }

  return { findings, next: { failures: 0, history } };
}

// ---- alert lifecycle ---------------------------------------------------------
// prevAlerts: { [id]: { level, title, sinceMs, lastSentMs } }
// Returns { alerts, events } where events is what should be delivered this run:
//   { type: "fire" | "escalate" | "realert" | "resolve", id, level, title, sinceMs }
export function reconcileAlerts(findings, prevAlerts, nowMs, realertMs) {
  const prev = prevAlerts || {};
  const alerts = {};
  const events = [];

  for (const f of findings) {
    const p = prev[f.id];
    if (!p) {
      alerts[f.id] = { level: f.level, title: f.title, sinceMs: nowMs, lastSentMs: nowMs };
      events.push({ type: "fire", id: f.id, level: f.level, title: f.title, sinceMs: nowMs });
    } else if (p.level === "warn" && f.level === "page") {
      alerts[f.id] = { level: "page", title: f.title, sinceMs: p.sinceMs, lastSentMs: nowMs };
      events.push({ type: "escalate", id: f.id, level: "page", title: f.title, sinceMs: p.sinceMs });
    } else if (nowMs - Number(p.lastSentMs || 0) >= realertMs) {
      alerts[f.id] = { level: f.level, title: f.title, sinceMs: p.sinceMs, lastSentMs: nowMs };
      events.push({ type: "realert", id: f.id, level: f.level, title: f.title, sinceMs: p.sinceMs });
    } else {
      // Still active, recently notified: keep state, refresh title, stay quiet.
      alerts[f.id] = { level: f.level, title: f.title, sinceMs: p.sinceMs, lastSentMs: p.lastSentMs };
    }
  }

  for (const [id, p] of Object.entries(prev)) {
    if (!alerts[id]) {
      events.push({ type: "resolve", id, level: p.level, title: p.title, sinceMs: p.sinceMs });
    }
  }

  return { alerts, events };
}

function durationLabel(sinceMs, nowMs) {
  const mins = Math.max(0, Math.round((nowMs - Number(sinceMs || nowMs)) / 60000));
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h${mins % 60}m`;
}

export function formatEventLines(events, nowMs) {
  const order = { fire: 0, escalate: 0, realert: 1, resolve: 2 };
  const sorted = events.slice().sort((a, b) => {
    const byType = (order[a.type] ?? 3) - (order[b.type] ?? 3);
    if (byType !== 0) return byType;
    // pages before warns
    return (a.level === "page" ? 0 : 1) - (b.level === "page" ? 0 : 1);
  });
  return sorted.map((e) => {
    const lvl = e.level === "page" ? "PAGE" : "WARN";
    if (e.type === "resolve") {
      return `[RESOLVED] ${e.title} (was ${lvl.toLowerCase()}, active ${durationLabel(e.sinceMs, nowMs)})`;
    }
    if (e.type === "realert") {
      return `[${lvl} ${durationLabel(e.sinceMs, nowMs)}] ${e.title}`;
    }
    if (e.type === "escalate") {
      return `[${lvl}] (escalated from warn) ${e.title}`;
    }
    return `[${lvl}] ${e.title}`;
  });
}

// Discord message content hard limit is 2000 chars; stay under it.
export function formatWebhookPayload(events, format, nowMs) {
  const lines = formatEventLines(events, nowMs);
  let text = ["cathedral watcher", ...lines].join("\n");
  if (text.length > 1900) text = text.slice(0, 1900) + "\n...(truncated)";
  if (format === "slack") return { text };
  if (format === "json") return { source: "cathedral-alert-watcher", events };
  return { content: text }; // discord (default)
}
