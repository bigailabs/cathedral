import assert from "node:assert/strict";
import fs from "node:fs";

const text = fs.readFileSync("deploy/railway-split.ps1", "utf8");

function block(name) {
  const match = text.match(new RegExp(`\\$${name}\\s*=\\s*\\[ordered\\]@\\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `missing ${name} block`);
  return match[1];
}

function assertVar(source, key, value, label) {
  const pattern = new RegExp(`"${key}"\\s*=\\s*"${value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`);
  assert.match(source, pattern, `${label} missing ${key}=${value}`);
}

const read = block("ReadVars");
assertVar(read, "CATHEDRAL_SERVICE_ROLE", "read", "read service");
assertVar(read, "CATHEDRAL_REFILL_ENABLED", "false", "read service");
assertVar(read, "CATHEDRAL_SEED_ON_BOOT", "false", "read service");
assertVar(read, "WEB_CONCURRENCY", "2", "read service");
assertVar(read, "CATHEDRAL_PM_READ_HARD_CAP", "128", "read service");
assertVar(read, "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS", "4000", "read service");
assertVar(read, "CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED", "1", "read service");
assertVar(read, "CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS", "60", "read service");
assertVar(read, "CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS", "900", "read service");
assertVar(read, "CATHEDRAL_RECENT_SNAPSHOT_LIMIT", "50", "read service");
assertVar(read, "CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT", "50", "read service");

const submit = block("SubmitVars");
assertVar(submit, "CATHEDRAL_SERVICE_ROLE", "submit", "submit service");
assertVar(submit, "CATHEDRAL_REFILL_ENABLED", "false", "submit service");
assertVar(submit, "CATHEDRAL_SEED_ON_BOOT", "false", "submit service");
assertVar(submit, "CATHEDRAL_SUBMIT_HARD_CAP", "8", "submit service");
assertVar(submit, "CATHEDRAL_SUBMIT_MAX_CONCURRENCY", "24", "submit service");
assertVar(submit, "WEB_CONCURRENCY", "2", "submit service");
assertVar(submit, "CATHEDRAL_PM_READ_HARD_CAP", "128", "submit service");
assertVar(submit, "CATHEDRAL_THREADPOOL_TOKENS", "32", "submit service");
assertVar(submit, "CATHEDRAL_PG_POOL_MAX", "32", "submit service");
assertVar(submit, "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS", "4000", "submit service");

const worker = block("WorkerVars");
assertVar(worker, "CATHEDRAL_SERVICE_ROLE", "worker", "worker service");
assertVar(worker, "CATHEDRAL_REFILL_ENABLED", "true", "worker service");
assertVar(worker, "CATHEDRAL_SINGLETON_RETRY_SECS", "15", "worker service");
assertVar(worker, "CATHEDRAL_THREADPOOL_TOKENS", "8", "worker service");
assertVar(worker, "CATHEDRAL_PG_POOL_MAX", "8", "worker service");

assert.match(text, /CATHEDRAL_CNF_TOKEN_SECRET/, "shared CNF token warning/apply path must stay present");
assert.match(text, /structured 503 on live-query failure/, "post-apply verification message must mention recent degraded fallback");
assert.match(text, /RailwayExe/, "script must support an explicit Railway CLI path");
assert.match(text, /Resolve-RailwayCli/, "script must resolve Railway CLI outside PATH");
assert.match(text, /railway login/, "apply path must fail clearly when Railway auth is stale");
assert.match(text, /railway link/, "apply path must fail clearly when checkout is not linked");

console.log("railway split config tests passed");
