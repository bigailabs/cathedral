import assert from "node:assert/strict";
import fs from "node:fs";

const text = fs.readFileSync("deploy/launch-preflight.ps1", "utf8");

assert.match(text, /gh" @\(\s*"pr", "view"/s, "preflight must inspect PR state");
assert.match(text, /rev-list", "--left-right", "--count", "origin\/main\.\.\.HEAD"/, "preflight must compare against origin/main");
assert.match(text, /Railway CLI is not authenticated/, "preflight must fail clearly on stale Railway auth/link");
assert.match(text, /railway login/, "preflight must tell operator to refresh Railway auth");
assert.match(text, /railway link/, "preflight must tell operator to link the project");
assert.match(text, /post-deploy-smoke\.ps1/, "preflight must validate the final smoke command");
assert.match(text, /RequireFinalGate/, "preflight must use the enforced final gate");
assert.match(text, /Microsoft Store stub/, "preflight must reject Windows Store Python stub");
assert.ok(fs.existsSync("deploy/python-wsl.cmd"), "Windows WSL Python wrapper must exist for post-deploy smoke");

assert.doesNotMatch(text, /gh"\s+@\("pr", "merge"/, "preflight must not merge PRs");
assert.doesNotMatch(text, /railway"\s+@\("variables"/, "preflight must not mutate Railway variables");
assert.doesNotMatch(text, /railway"\s+@\("deploy"/, "preflight must not deploy Railway services");

console.log("launch preflight tests passed");
