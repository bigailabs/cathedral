import { pathToFileURL } from "node:url";

const CF_BASE = "https://api.cloudflare.com/client/v4";
const DEFAULT_ZONE = "cathedral.computer";
const DEFAULT_HOST = "api.cathedral.computer";
const RULE_DESCRIPTION = "Cathedral API edge hot-path bypass";

export function apiHotPathExpression(host = DEFAULT_HOST) {
  return `(http.host eq "${host}" and (` +
    `starts_with(http.request.uri.path, "/health") or ` +
    `http.request.uri.path eq "/.well-known/cathedral-jwks.json" or ` +
    `starts_with(http.request.uri.path, "/v1/synthetic-boolean/") or ` +
    `starts_with(http.request.uri.path, "/v1/agents/submit") or ` +
    `starts_with(http.request.uri.path, "/v1/leaderboard/") or ` +
    `starts_with(http.request.uri.path, "/v1/validator/weights/next") or ` +
    `starts_with(http.request.uri.path, "/api/cathedral/v1/synthetic-boolean/") or ` +
    `starts_with(http.request.uri.path, "/api/cathedral/v1/agents/submit") or ` +
    `starts_with(http.request.uri.path, "/api/cathedral/v1/leaderboard/") or ` +
    `starts_with(http.request.uri.path, "/api/cathedral/v1/validator/weights/next")` +
  `))`;
}

export function bypassRule(host = DEFAULT_HOST) {
  return {
    description: RULE_DESCRIPTION,
    action: "skip",
    action_parameters: {
      phases: [
        "http_ratelimit",
        "http_request_firewall_managed",
      ],
      products: [
        "rateLimit",
        "waf",
        "bic",
        "securityLevel",
      ],
    },
    expression: apiHotPathExpression(host),
    enabled: true,
  };
}

async function cfRequest(token, path, options = {}) {
  const res = await fetch(`${CF_BASE}${path}`, {
    ...options,
    headers: {
      "authorization": `Bearer ${token}`,
      "content-type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { success: false, errors: [{ message: text || res.statusText }] };
  }
  if (!res.ok || body.success === false) {
    const errors = (body.errors || []).map((err) => err.message || JSON.stringify(err)).join("; ");
    const error = new Error(`Cloudflare API ${res.status}: ${errors || res.statusText}`);
    error.status = res.status;
    error.body = body;
    throw error;
  }
  return body.result;
}

async function getZoneId(token, zoneName) {
  const zones = await cfRequest(token, `/zones?name=${encodeURIComponent(zoneName)}&status=active`);
  if (!Array.isArray(zones) || zones.length !== 1) {
    const got = Array.isArray(zones) ? zones.length : "non-array";
    throw new Error(
      `Token cannot see exactly one active zone for ${zoneName}; got ${got}. ` +
      `Grant Zone:Read on this zone/account before applying the bypass.`,
    );
  }
  return zones[0].id;
}

async function getCustomEntrypoint(token, zoneId) {
  try {
    return await cfRequest(token, `/zones/${zoneId}/rulesets/phases/http_request_firewall_custom/entrypoint`);
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

function existingBypassRule(ruleset) {
  return (ruleset?.rules || []).find((rule) => rule.description === RULE_DESCRIPTION);
}

function printPlan({ zoneName, host, apply, ruleset, existing }) {
  const rule = bypassRule(host);
  console.log(`cloudflare-api-bypass zone=${zoneName} host=${host} mode=${apply ? "apply" : "dry-run"}`);
  console.log(`expression=${apiHotPathExpression(host)}`);
  console.log(`skip_phases=${rule.action_parameters.phases.join(",")}`);
  console.log(`skip_products=${rule.action_parameters.products.join(",")}`);
  if (!ruleset) console.log("custom_entrypoint=missing");
  else console.log(`custom_entrypoint=${ruleset.id} rules=${(ruleset.rules || []).length}`);
  if (existing) console.log(`existing_rule=${existing.id || "present"}`);
}

function sameRule(existing, desired) {
  return existing.action === desired.action &&
    existing.expression === desired.expression &&
    existing.enabled === desired.enabled &&
    JSON.stringify(existing.action_parameters || {}) === JSON.stringify(desired.action_parameters || {});
}

function firstRuleId(ruleset) {
  return (ruleset?.rules || [])[0]?.id || "";
}

function rulePayload(rule, ruleset, existingId = "") {
  const first = firstRuleId(ruleset);
  if (first && first !== existingId) {
    return { ...rule, position: { before: first } };
  }
  return rule;
}

async function ensureBypass({ token, zoneName, host, apply }) {
  const zoneId = await getZoneId(token, zoneName);
  const ruleset = await getCustomEntrypoint(token, zoneId);
  const existing = existingBypassRule(ruleset);
  const rule = bypassRule(host);
  const needsUpdate = existing && !sameRule(existing, rule);
  const needsMove = existing && firstRuleId(ruleset) && firstRuleId(ruleset) !== existing.id;
  printPlan({ zoneName, host, apply, ruleset, existing });

  if (existing) {
    console.log(`rule_drift=${needsUpdate ? "yes" : "no"}`);
    console.log(`rule_first=${needsMove ? "no" : "yes"}`);
    if (!needsUpdate && !needsMove) {
      console.log("status=already_present");
      return;
    }
    if (!apply) {
      console.log("status=dry_run_update_needed");
      console.log("rerun_with=--apply");
      return;
    }
    const updated = await cfRequest(token, `/zones/${zoneId}/rulesets/${ruleset.id}/rules/${existing.id}`, {
      method: "PATCH",
      body: JSON.stringify(rulePayload(rule, ruleset, existing.id)),
    });
    console.log(`status=updated_rule ruleset=${updated.id}`);
    return;
  }
  if (!apply) {
    console.log("status=dry_run_only");
    console.log("rerun_with=--apply");
    return;
  }

  if (!ruleset) {
    const created = await cfRequest(token, `/zones/${zoneId}/rulesets`, {
      method: "POST",
      body: JSON.stringify({
        name: "Cathedral API edge bypass",
        description: "Skip Cloudflare rate-limit/WAF products for routed Cathedral API hot paths.",
        kind: "zone",
        phase: "http_request_firewall_custom",
        rules: [rule],
      }),
    });
    console.log(`status=created_entrypoint ruleset=${created.id}`);
    return;
  }

  const updated = await cfRequest(token, `/zones/${zoneId}/rulesets/${ruleset.id}/rules`, {
    method: "POST",
    body: JSON.stringify(rulePayload(rule, ruleset)),
  });
  console.log(`status=created_rule ruleset=${updated.id}`);
}

async function main() {
  const args = new Set(process.argv.slice(2));
  const apply = args.has("--apply");
  const token = process.env.CLOUDFLARE_API_TOKEN;
  const zoneName = process.env.CLOUDFLARE_ZONE_NAME || DEFAULT_ZONE;
  const host = process.env.CLOUDFLARE_API_HOST || DEFAULT_HOST;

  if (!token) {
    console.error("CLOUDFLARE_API_TOKEN is required. Refusing to read token from command-line args.");
    process.exitCode = 2;
    return;
  }

  await ensureBypass({ token, zoneName, host, apply });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
