import assert from "node:assert/strict";
import fs from "node:fs";

function routeLines(path) {
  const text = fs.readFileSync(path, "utf8");
  const lines = text.split(/\r?\n/);
  const out = [];
  let inRoutes = false;
  for (const line of lines) {
    if (line.match(/^\s*routes\s*=\s*\[/)) {
      inRoutes = true;
      continue;
    }
    if (inRoutes && line.match(/^\s*\]\s*$/)) break;
    if (inRoutes) out.push(line);
  }
  return out;
}

function activePatterns(path) {
  const out = [];
  for (const line of routeLines(path)) {
    const match = line.match(/^\s*\{\s*pattern\s*=\s*"([^"]+)"/);
    if (match) out.push(match[1]);
  }
  return new Set(out);
}

function commentedPatterns(path) {
  const out = [];
  for (const line of routeLines(path)) {
    const match = line.match(/^\s*#\s*\{\s*pattern\s*=\s*"([^"]+)"/);
    if (match) out.push(match[1]);
  }
  return new Set(out);
}

function assertHasAll(patterns, expected, label) {
  for (const pattern of expected) {
    assert.ok(patterns.has(pattern), `${label} missing route pattern: ${pattern}`);
  }
}

function assertNoBroadCatchAll(patterns, label) {
  for (const pattern of patterns) {
    assert.notEqual(pattern, "api.cathedral.computer/*", `${label} must not attach catch-all`);
    assert.notEqual(
      pattern,
      "api.cathedral.computer/v1/synthetic-boolean/*",
      `${label} must not attach broad synthetic-boolean prefix`,
    );
    assert.notEqual(
      pattern,
      "api.cathedral.computer/v1/verifiable-sat/*",
      `${label} must not attach broad verifiable-SAT prefix`,
    );
  }
}

const main = activePatterns("deploy/edge-router/wrangler.toml");
assertNoBroadCatchAll(main, "main edge router");
assertHasAll(
  main,
  [
    "api.cathedral.computer/v1/verifiable-sat/coinbase/status*",
    "api.cathedral.computer/v1/verifiable-sat/coinbase/challenge*",
    "api.cathedral.computer/v1/verifiable-sat/coinbase/verify*",
    "api.cathedral.computer/v1/verifiable-sat/coinbase/submit*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/status*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/challenge*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/verify*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/submit*",
  ],
  "main edge router",
);

const boardFailover = commentedPatterns("deploy/edge-router/board-failover/wrangler.toml");
assertNoBroadCatchAll(boardFailover, "board failover");
assertHasAll(
  boardFailover,
  [
    "api.cathedral.computer/v1/verifiable-sat/coinbase/challenge*",
    "api.cathedral.computer/v1/verifiable-sat/coinbase/verify*",
    "api.cathedral.computer/v1/verifiable-sat/coinbase/submit*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/challenge*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/verify*",
    "api.cathedral.computer/api/cathedral/v1/verifiable-sat/coinbase/submit*",
  ],
  "board failover",
);

console.log("edge-router route config tests passed");
