import assert from "node:assert/strict";
import {
  boardLkgKey,
  canonicalPath,
  classifyBoardRequest,
  handleRequest,
  originForTier,
  originRequest,
  originTimeoutForTier,
  preflightResponse,
  serveBoardStale,
} from "./worker.js";

// Minimal in-memory KV mock matching the Cloudflare KV surface this worker uses
// (get-with-metadata + put-with-metadata). Used to exercise the opt-in board LKG.
function makeKv() {
  const store = new Map();
  return {
    store,
    async getWithMetadata(key) {
      return store.get(key) || null;
    },
    async put(key, value, opts) {
      store.set(key, { value, metadata: (opts && opts.metadata) || {} });
    },
  };
}

// Drain ctx.waitUntil promises so KV mirror writes complete before assertions.
function makeCtx() {
  const pending = [];
  return {
    ctx: { waitUntil: (p) => pending.push(p) },
    settle: () => Promise.all(pending),
  };
}

// --- canonicalPath strips the legacy prefix and trailing slash ---
assert.equal(canonicalPath("/v1/synthetic-boolean/current-challenge"), "/v1/synthetic-boolean/current-challenge");
assert.equal(canonicalPath("/v1/synthetic-boolean/current-challenge/"), "/v1/synthetic-boolean/current-challenge");
assert.equal(
  canonicalPath("/api/cathedral/v1/synthetic-boolean/current-challenge"),
  "/v1/synthetic-boolean/current-challenge",
);

// --- Tier-1 READ-role board reads classify as "board" (read/publisher origin) ---
for (const path of [
  "/v1/synthetic-boolean/active-challenges",
  "/v1/synthetic-boolean/challenge-broadcast",
  "/v1/synthetic-boolean/current-challenge",
  "/v1/synthetic-boolean/per-miner/status",
  "/v1/synthetic-boolean/per-miner/summary",
]) {
  assert.deepEqual(classifyBoardRequest("GET", path), { tier: "board", path }, `board GET ${path}`);
  assert.deepEqual(
    classifyBoardRequest("GET", `/api/cathedral${path}`),
    { tier: "board", path },
    `board legacy GET ${path}`,
  );
}

// --- SUBMIT-role paths classify as "submit" (submit origin), NOT board/read.
//     This is the must-fix: per-miner/cnf, per-miner/challenges, active-cnf and
//     the private /v1/challenges/* fetch + POST submit are served by the SUBMIT
//     origin in prod; a read origin 404s them. They must never be "board". ---
for (const path of [
  "/v1/synthetic-boolean/active-cnf",
  "/v1/synthetic-boolean/per-miner/challenges",
  "/v1/synthetic-boolean/per-miner/cnf",
]) {
  const got = classifyBoardRequest("GET", path);
  assert.equal(got.tier, "submit", `submit-role GET ${path} must classify as submit`);
  assert.notEqual(got.tier, "board", `submit-role GET ${path} must NOT classify as board (read origin)`);
  // legacy /api/cathedral form must also be submit, not board.
  const legacy = classifyBoardRequest("GET", `/api/cathedral${path}`);
  assert.equal(legacy.tier, "submit", `submit-role legacy GET ${path} must classify as submit`);
}
// Private challenge fetch prefix is submit-role.
assert.equal(
  classifyBoardRequest("GET", "/v1/challenges/abc-123").tier,
  "submit",
  "/v1/challenges/* must classify as submit",
);
// POST submit is submit-role.
assert.deepEqual(
  classifyBoardRequest("POST", "/v1/agents/submit"),
  { tier: "submit", path: "/v1/agents/submit" },
);

// --- The slow leaderboard hog is LEFT on the leaderboard (read) origin ---
assert.deepEqual(
  classifyBoardRequest("GET", "/v1/leaderboard/recent"),
  { tier: "leaderboard", path: "/v1/leaderboard/recent" },
);
assert.deepEqual(
  classifyBoardRequest("GET", "/v1/leaderboard/top"),
  { tier: "leaderboard", path: "/v1/leaderboard/top" },
);

// --- Unknown / non-GET routes are default-denied ---
assert.deepEqual(
  classifyBoardRequest("GET", "/v1/leaderboard/explain"),
  { tier: "none", path: "/v1/leaderboard/explain" },
);
assert.deepEqual(
  classifyBoardRequest("POST", "/v1/synthetic-boolean/current-challenge"),
  { tier: "none", path: "/v1/synthetic-boolean/current-challenge" },
);
assert.deepEqual(
  classifyBoardRequest("GET", "/v1/unknown"),
  { tier: "none", path: "/v1/unknown" },
);

// --- ROLE SPLIT: origin selection. A submit-role path must aim at the submit
//     origin, never a read origin. This test FAILS if submit is pointed at the
//     read/board origin. ---
{
  const env = {
    BOARD_ORIGIN: "https://read.example",
    LEADERBOARD_ORIGIN: "https://read.example",
    SUBMIT_ORIGIN: "https://submit.example",
  };
  for (const path of [
    "/v1/synthetic-boolean/active-cnf",
    "/v1/synthetic-boolean/per-miner/challenges",
    "/v1/synthetic-boolean/per-miner/cnf",
    "/v1/challenges/abc",
  ]) {
    const { tier } = classifyBoardRequest("GET", path);
    const origin = originForTier(env, tier);
    assert.equal(origin.base, "https://submit.example", `${path} must aim at submit origin`);
    assert.notEqual(origin.base, env.BOARD_ORIGIN, `${path} must NOT aim at the read/board origin`);
  }
  // board + leaderboard stay on the read origin.
  assert.equal(originForTier(env, "board").base, "https://read.example");
  assert.equal(originForTier(env, "leaderboard").base, "https://read.example");
}

// --- Default submit origin is submit.cathedral.computer, never the read host ---
{
  const origin = originForTier({}, "submit");
  assert.equal(origin.base, "https://submit.cathedral.computer");
  assert.doesNotMatch(origin.base, /read\.cathedral\.computer/);
}

// --- Board reads get the tight timeout; leaderboard keeps the loose one;
//     submit gets the submit timeout ---
assert.equal(originTimeoutForTier({}, "board"), 4500);
assert.equal(originTimeoutForTier({}, "leaderboard"), 9000);
assert.equal(originTimeoutForTier({}, "submit"), 10000);
assert.equal(originTimeoutForTier({ BOARD_ORIGIN_TIMEOUT_MS: "3000" }, "board"), 3000);
assert.equal(originTimeoutForTier({ LEADERBOARD_ORIGIN_TIMEOUT_MS: "12000" }, "leaderboard"), 12000);
assert.equal(originTimeoutForTier({ SUBMIT_ORIGIN_TIMEOUT_MS: "15000" }, "submit"), 15000);

// --- originRequest rewrites host + canonicalizes path, preserving query ---
{
  const request = new Request(
    "https://api.cathedral.computer/api/cathedral/v1/synthetic-boolean/current-challenge?tier=1",
  );
  const origin = originRequest(request, "https://board-publisher.up.railway.app", {
    hostHeader: "api.cathedral.computer",
  });
  assert.equal(
    origin.url,
    "https://board-publisher.up.railway.app/v1/synthetic-boolean/current-challenge?tier=1",
  );
  assert.equal(origin.headers.get("host"), "api.cathedral.computer");
}

// --- handleRequest routes board reads to BOARD_ORIGIN, not LEADERBOARD_ORIGIN ---
{
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (req) => {
    seen.push(new URL(req.url).origin);
    return new Response('{"ok":true}', {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const env = {
      BOARD_ORIGIN: "https://board.example",
      LEADERBOARD_ORIGIN: "https://leaderboard.example",
      SUBMIT_ORIGIN: "https://submit.example",
    };
    const boardResp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/active-challenges"),
      env,
    );
    assert.equal(boardResp.status, 200);
    assert.equal(boardResp.headers.get("X-Cathedral-Board-Tier"), "board");
    assert.equal(seen[seen.length - 1], "https://board.example");

    const lbResp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/leaderboard/recent?limit=20"),
      env,
    );
    assert.equal(lbResp.status, 200);
    assert.equal(lbResp.headers.get("X-Cathedral-Board-Tier"), "leaderboard");
    assert.equal(seen[seen.length - 1], "https://leaderboard.example");

    // submit-role GET goes to the submit origin, not the board/read origin.
    const cnfResp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-1"),
      env,
    );
    assert.equal(cnfResp.status, 200);
    assert.equal(cnfResp.headers.get("X-Cathedral-Board-Tier"), "submit");
    assert.equal(seen[seen.length - 1], "https://submit.example");
    assert.notEqual(seen[seen.length - 1], "https://board.example");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- OPTIONS preflight is answered locally with 204 + CORS headers, without
//     touching any origin ---
{
  let touched = false;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    touched = true;
    return new Response("");
  };
  try {
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/per-miner/cnf", {
        method: "OPTIONS",
      }),
    );
    assert.equal(resp.status, 204);
    assert.equal(resp.headers.get("Access-Control-Allow-Origin"), "*");
    assert.match(resp.headers.get("Access-Control-Allow-Methods"), /OPTIONS/);
    assert.match(resp.headers.get("Access-Control-Allow-Headers"), /x-cathedral-signature/);
    assert.equal(touched, false, "preflight must not hit an origin");
  } finally {
    globalThis.fetch = realFetch;
  }
  // preflightResponse is exported and self-consistent.
  assert.equal(preflightResponse().status, 204);
}

// --- A board-origin failure returns 504 board_origin_unavailable, and does NOT
//     touch the leaderboard origin (isolation holds in both directions) ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("connect timeout");
  };
  try {
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/current-challenge"),
      { BOARD_ORIGIN: "https://board.example" },
    );
    assert.equal(resp.status, 504);
    const body = await resp.json();
    assert.equal(body.error, "board_origin_unavailable");

    // submit-origin failure reports submit_origin_unavailable, not board.
    const submitResp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/active-cnf"),
      { SUBMIT_ORIGIN: "https://submit.example" },
    );
    assert.equal(submitResp.status, 504);
    assert.equal((await submitResp.json()).error, "submit_origin_unavailable");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- Unknown route default-denies with 404 ---
{
  const resp = await handleRequest(
    new Request("https://api.cathedral.computer/v1/unknown"),
  );
  assert.equal(resp.status, 404);
  assert.match(await resp.text(), /route_not_served_by_board_failover/);
}

// --- OPT-IN board LKG: a healthy board response is mirrored to KV, then served
//     as last-known-good when the origin later fails (degrade to stale) ---
{
  const realFetch = globalThis.fetch;
  const kv = makeKv();
  const path = "/v1/synthetic-boolean/active-challenges";
  try {
    // 1) Healthy origin -> response passes through AND is mirrored to KV.
    globalThis.fetch = async () =>
      new Response('{"items":[{"id":"c1"}]}', {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    const okCtx = makeCtx();
    const ok = await handleRequest(
      new Request(`https://api.cathedral.computer${path}`),
      { BOARD_ORIGIN: "https://board.example", BOARD_LKG: kv },
      okCtx.ctx,
    );
    assert.equal(ok.status, 200);
    assert.equal(await ok.text(), '{"items":[{"id":"c1"}]}');
    await okCtx.settle();
    const stored = kv.store.get(boardLkgKey(path));
    assert.ok(stored && stored.value === '{"items":[{"id":"c1"}]}', "board body mirrored to KV");

    // 2) Origin now throws -> serve the last-known-good from KV, marked stale,
    //    NOT a 504.
    globalThis.fetch = async () => {
      throw new Error("connect timeout");
    };
    const staleResp = await handleRequest(
      new Request(`https://api.cathedral.computer${path}`),
      { BOARD_ORIGIN: "https://board.example", BOARD_LKG: kv },
      makeCtx().ctx,
    );
    assert.equal(staleResp.status, 200, "stale fallback served, not 504");
    assert.equal(staleResp.headers.get("x-cathedral-board-source"), "stale_fallback");
    assert.equal(staleResp.headers.get("cache-control"), "no-store");
    assert.equal(await staleResp.text(), '{"items":[{"id":"c1"}]}');

    // 3) Origin 5xx -> also degrades to the stored snapshot rather than the error.
    globalThis.fetch = async () => new Response("boom", { status: 503 });
    const fiveXx = await handleRequest(
      new Request(`https://api.cathedral.computer${path}`),
      { BOARD_ORIGIN: "https://board.example", BOARD_LKG: kv },
      makeCtx().ctx,
    );
    assert.equal(fiveXx.status, 200, "5xx degrades to stale snapshot");
    assert.equal(fiveXx.headers.get("x-cathedral-board-source"), "stale_fallback");
    assert.match(fiveXx.headers.get("x-cathedral-fallback-reason"), /origin_status_503/);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- FLAG-OFF (no BOARD_LKG binding): behavior is unchanged — an origin failure
//     still returns 504, nothing is cached, serveBoardStale yields null ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("connect timeout");
  };
  try {
    const resp = await handleRequest(
      new Request("https://api.cathedral.computer/v1/synthetic-boolean/active-challenges"),
      { BOARD_ORIGIN: "https://board.example" }, // no BOARD_LKG -> LKG disabled
      makeCtx().ctx,
    );
    assert.equal(resp.status, 504, "no KV binding => unchanged 504 behavior");
    assert.equal((await resp.json()).error, "board_origin_unavailable");
  } finally {
    globalThis.fetch = realFetch;
  }
  // serveBoardStale with no binding returns null (caller falls through to 504).
  assert.equal(
    await serveBoardStale({}, "/v1/synthetic-boolean/active-challenges", "origin_error"),
    null,
  );
}

console.log("board-failover worker tests passed");
