import { pathToFileURL } from "node:url";

const DEFAULT_API_BASE = "https://api.cathedral.computer";
const DEFAULT_READ_BASE = "https://read.cathedral.computer";
const DEFAULT_SUBMIT_BASE = "https://submit.cathedral.computer";

function baseUrl(value, fallback) {
  return (value || fallback).replace(/\/+$/, "");
}

function bodyKind(body) {
  const text = String(body || "");
  if (/Error\s+1027/i.test(text) || /owner has reached their plan limits/i.test(text)) return "cloudflare_worker_plan_limit";
  if (/temporarily rate limited/i.test(text)) return "cloudflare_rate_limited";
  if (/route_not_served_by_cathedral_edge/i.test(text)) return "edge_route_denied";
  if (/route_not_served_by_.*_role/i.test(text)) return "service_role_denied";
  if (/^\s*\{/.test(text)) return "json";
  return text ? "html_or_text" : "empty";
}

function okJsonRole(probe, role) {
  return probe.status === 200 && probe.bodyKind === "json" && probe.body.includes(`"service_role":"${role}"`);
}

export function classifyEdgeHealth(probes) {
  const api = probes.apiReady;
  const read = probes.readReady;
  const submit = probes.submitReady;

  if (api.bodyKind === "exception" || read.bodyKind === "exception" || submit.bodyKind === "exception") {
    return {
      status: "network_probe_error",
      severity: "investigate",
      summary: "The diagnostic machine could not complete one or more endpoint probes.",
      action: "Retry from another network before restarting services; if repeated, inspect DNS/connectivity for the affected hostname.",
    };
  }

  const readOk = okJsonRole(read, "read");
  const submitOk = okJsonRole(submit, "submit");

  if (!readOk || !submitOk) {
    return {
      status: "origin_unhealthy",
      severity: "critical",
      summary: "One or both split Railway origins are unhealthy.",
      action: "Fix read/submit Railway services before debugging Cloudflare.",
    };
  }

  if (api.status === 429 && api.bodyKind === "cloudflare_rate_limited") {
    return {
      status: "cloudflare_zone_rate_limited",
      severity: "operator_action",
      summary: "The API edge hostname is being rate-limited by Cloudflare before Cathedral JSON reaches the client.",
      action: "Add a targeted Cloudflare rate-limit/WAF bypass for the Cathedral API hot paths, or keep miners on read/submit split endpoints.",
    };
  }

  if (api.status === 429 && api.bodyKind === "cloudflare_worker_plan_limit") {
    return {
      status: "cloudflare_worker_plan_limit",
      severity: "operator_action",
      summary: "The API edge hostname is blocked because the Cloudflare Worker has reached its request plan limit.",
      action: "Upgrade the Cloudflare Workers plan or disable the Worker routes; keep miners on read/submit split endpoints until api.cathedral.computer is healthy.",
    };
  }

  if (api.server.toLowerCase().includes("cloudflare") && !api.edge) {
    return {
      status: "cloudflare_policy_or_route_gap",
      severity: "operator_action",
      summary: "The API edge hostname is passing through Cloudflare but not returning Worker edge headers.",
      action: "Check Worker route attachment and Cloudflare zone security/rate-limit rules for the API edge hostname.",
    };
  }

  if (api.status === 200 && api.edge === "BYPASS") {
    return {
      status: "healthy",
      severity: "ok",
      summary: "The API edge hostname reaches the Worker and read origin readiness is healthy.",
      action: "No action needed for edge readiness.",
    };
  }

  return {
    status: "unknown_edge_state",
    severity: "investigate",
    summary: "Split origins are healthy, but the API edge hostname did not match a known healthy/failure pattern.",
    action: "Run route-map and inspect Cloudflare Worker routes and zone security events.",
  };
}

async function probe(url, { timeoutMs }) {
  const started = Date.now();
  let res;
  let body = "";
  try {
    res = await fetch(url, {
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
    body = await res.text();
  } catch (error) {
    return {
      url,
      status: 0,
      ms: Date.now() - started,
      edge: "",
      server: "",
      body: String(error && error.message || error),
      bodyKind: "exception",
    };
  }
  return {
    url,
    status: res.status,
    ms: Date.now() - started,
    edge: res.headers.get("x-cathedral-edge-cache") || "",
    server: res.headers.get("server") || "",
    body,
    bodyKind: bodyKind(body),
  };
}

function printProbe(name, probe) {
  console.log(
    `${name.padEnd(8)} ${String(probe.status).padStart(3)} ` +
    `${String(probe.ms).padStart(5)}ms ` +
    `edge=${probe.edge || "-"} server=${probe.server || "-"} body=${probe.bodyKind}`,
  );
}

async function main() {
  const timeoutMs = Number(process.env.CATHEDRAL_EDGE_TIMEOUT_MS || 10000);
  const apiBase = baseUrl(process.env.CATHEDRAL_API_BASE_URL, DEFAULT_API_BASE);
  const readBase = baseUrl(process.env.CATHEDRAL_READ_BASE_URL, DEFAULT_READ_BASE);
  const submitBase = baseUrl(process.env.CATHEDRAL_SUBMIT_BASE_URL, DEFAULT_SUBMIT_BASE);

  const probes = {
    apiReady: await probe(`${apiBase}/health/ready`, { timeoutMs }),
    readReady: await probe(`${readBase}/health/ready`, { timeoutMs }),
    submitReady: await probe(`${submitBase}/health/ready`, { timeoutMs }),
  };
  const diagnosis = classifyEdgeHealth(probes);

  console.log(`edge-diagnose api=${apiBase} read=${readBase} submit=${submitBase}`);
  printProbe("api", probes.apiReady);
  printProbe("read", probes.readReady);
  printProbe("submit", probes.submitReady);
  console.log(`status=${diagnosis.status}`);
  console.log(`severity=${diagnosis.severity}`);
  console.log(`summary=${diagnosis.summary}`);
  console.log(`action=${diagnosis.action}`);

  if (diagnosis.severity === "critical") process.exitCode = 2;
  else if (diagnosis.severity !== "ok") process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
