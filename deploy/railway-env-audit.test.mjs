import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = process.cwd();
const script = fs.readFileSync("deploy/railway-env-audit.ps1", "utf8");
const preflight = fs.readFileSync("deploy/launch-preflight.ps1", "utf8");

assert.match(script, /variable", "list"/, "audit must read Railway variables");
assert.match(script, /raw values/, "audit must document that Railway returns raw values");
assert.match(script, /CATHEDRAL_CNF_TOKEN_SECRET equal across read\/submit\/worker/, "audit must check shared CNF token equality");
assert.match(script, /CATHEDRAL_PG_STATEMENT_TIMEOUT_MS\s*=\s*"4000"/, "audit must require statement timeout");
assert.match(script, /CATHEDRAL_SUBMIT_HARD_CAP\s*=\s*"8"/, "audit must require submit cap 8");
assert.doesNotMatch(script, /variable", "set"/, "audit must not mutate Railway variables");
assert.doesNotMatch(script, /railway deploy/, "audit must not deploy Railway services");
assert.match(preflight, /railway-env-audit\.ps1/, "launch preflight must run the Railway env audit");

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cathedral-railway-env-audit-"));
const fakeJs = path.join(tmp, "fake-railway.js");
fs.writeFileSync(fakeJs, `#!/usr/bin/env node
const args = process.argv.slice(2);
const scenario = process.env.FAKE_RAILWAY_SCENARIO || "ok";
const shared = "fake-shared-value";

const read = {
  CATHEDRAL_SERVICE_ROLE: "read",
  CATHEDRAL_REFILL_ENABLED: "false",
  CATHEDRAL_SEED_ON_BOOT: "false",
  WEB_CONCURRENCY: "2",
  CATHEDRAL_PM_READ_HARD_CAP: "128",
  CATHEDRAL_PG_STATEMENT_TIMEOUT_MS: "4000",
  CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED: "1",
  CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS: "60",
  CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS: "900",
  CATHEDRAL_RECENT_SNAPSHOT_LIMIT: "50",
  CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT: "50",
  CATHEDRAL_CNF_TOKEN_SECRET: shared,
};
const submit = {
  CATHEDRAL_SERVICE_ROLE: "submit",
  CATHEDRAL_REFILL_ENABLED: "false",
  CATHEDRAL_SEED_ON_BOOT: "false",
  CATHEDRAL_SUBMIT_HARD_CAP: "8",
  CATHEDRAL_SUBMIT_MAX_CONCURRENCY: "24",
  WEB_CONCURRENCY: "2",
  CATHEDRAL_PM_READ_HARD_CAP: "128",
  CATHEDRAL_THREADPOOL_TOKENS: "32",
  CATHEDRAL_PG_POOL_MAX: "32",
  CATHEDRAL_PG_STATEMENT_TIMEOUT_MS: "4000",
  CATHEDRAL_CNF_TOKEN_SECRET: shared,
};
const worker = {
  CATHEDRAL_SERVICE_ROLE: "worker",
  CATHEDRAL_REFILL_ENABLED: "true",
  CATHEDRAL_SINGLETON_RETRY_SECS: "15",
  CATHEDRAL_THREADPOOL_TOKENS: "8",
  CATHEDRAL_PG_POOL_MAX: "8",
  CATHEDRAL_CNF_TOKEN_SECRET: shared,
};

if (args[0] === "status") {
  console.log("Project: cathedral");
  process.exit(0);
}

if (args[0] === "variable" && args[1] === "list") {
  const service = args[args.indexOf("--service") + 1];
  const map = { "cathedral-read": read, "cathedral-submit": submit, "cathedral-worker": worker }[service];
  if (!map) process.exit(12);
  if (scenario === "mismatch-token" && service === "cathedral-worker") {
    map.CATHEDRAL_CNF_TOKEN_SECRET = "different-fake-value";
  }
  if (scenario === "missing-timeout" && service === "cathedral-read") {
    delete map.CATHEDRAL_PG_STATEMENT_TIMEOUT_MS;
  }
  console.log(JSON.stringify(Object.entries(map).map(([name, value]) => ({ name, value }))));
  process.exit(0);
}

console.error("unexpected fake railway args", args.join(" "));
process.exit(99);
`);

let fakeRailway = fakeJs;
if (process.platform === "win32") {
  fakeRailway = path.join(tmp, "fake-railway.cmd");
  fs.writeFileSync(fakeRailway, `@echo off\r\nnode "%~dp0\\fake-railway.js" %*\r\n`);
} else {
  fs.chmodSync(fakeJs, 0o755);
}

function powershellBin() {
  const pwsh = spawnSync("pwsh", ["-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"], { encoding: "utf8" });
  if (!pwsh.error && pwsh.status === 0) return "pwsh";
  return "powershell";
}

function runAudit(scenario) {
  const ps = powershellBin();
  return spawnSync(ps, [
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "deploy/railway-env-audit.ps1",
    "-RailwayExe", fakeRailway,
  ], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, FAKE_RAILWAY_SCENARIO: scenario },
  });
}

const ok = runAudit("ok");
assert.equal(ok.status, 0, ok.stdout + ok.stderr);
assert.match(ok.stdout, /Railway env audit passed/);
assert.doesNotMatch(ok.stdout + ok.stderr, /fake-shared-value/, "audit output must not print shared CNF token");

const mismatch = runAudit("mismatch-token");
assert.notEqual(mismatch.status, 0, mismatch.stdout + mismatch.stderr);
assert.match(mismatch.stdout, /CATHEDRAL_CNF_TOKEN_SECRET differs across services/);
assert.doesNotMatch(mismatch.stdout + mismatch.stderr, /different-fake-value|fake-shared-value/, "failure output must not print raw token values");

const missing = runAudit("missing-timeout");
assert.notEqual(missing.status, 0, missing.stdout + missing.stderr);
assert.match(missing.stdout, /cathedral-read missing CATHEDRAL_PG_STATEMENT_TIMEOUT_MS/);

console.log("railway env audit tests passed");
