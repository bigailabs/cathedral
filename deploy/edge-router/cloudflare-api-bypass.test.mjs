import assert from "node:assert/strict";
import {
  apiHotPathExpression,
  bypassRule,
} from "./cloudflare-api-bypass.mjs";

const expression = apiHotPathExpression();
assert.match(expression, /http\.host eq "api\.cathedral\.computer"/);
assert.match(expression, /starts_with\(http\.request\.uri\.path, "\/v1\/synthetic-boolean\/"\)/);
assert.match(expression, /starts_with\(http\.request\.uri\.path, "\/v1\/agents\/submit"\)/);
assert.match(expression, /starts_with\(http\.request\.uri\.path, "\/api\/cathedral\/v1\/synthetic-boolean\/"\)/);
assert.match(expression, /starts_with\(http\.request\.uri\.path, "\/api\/cathedral\/v1\/agents\/submit"\)/);
assert.doesNotMatch(expression, /starts_with\(http\.request\.uri\.path, "\/api\/cathedral\/v1\/"\)/);

const rule = bypassRule();
assert.equal(rule.description, "Cathedral API edge hot-path bypass");
assert.equal(rule.action, "skip");
assert.equal(rule.enabled, true);
assert.deepEqual(rule.action_parameters.phases, [
  "http_ratelimit",
  "http_request_firewall_managed",
]);
assert.ok(rule.action_parameters.products.includes("rateLimit"));
assert.ok(rule.action_parameters.products.includes("waf"));
assert.equal(rule.expression, expression);

console.log("cloudflare-api-bypass tests passed");
