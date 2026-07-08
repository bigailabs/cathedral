const ORIGIN_HOST = "sandbox-v2-origin.cathedral.computer";
const ORIGIN_PORT = "8080";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

// Staged reopen: only canary hotkeys reach the origin while the edge-artifact
// read path (issue #363/#372 direction) is being stood up. Everyone else gets
// a fast edge 429 so the 4-vCPU sandbox origin never sees the stampede.
const CANARY_HOTKEYS = new Set([
  "5H1DGfCH5A6sRxiA64xdTG4SSpf7HpXtqGB8bohqnU1MWqv4", // Stitch dogfood miner
  "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", // //Alice dev key (E2E smoke)
]);

const LEGACY_PREFIX = "/api/cathedral";

function stripLegacyPrefix(pathname) {
  return pathname.startsWith(LEGACY_PREFIX) ? pathname.slice(LEGACY_PREFIX.length) : pathname;
}

function isGatedMinerPath(url, method) {
  // Fairness: while reopen is staged, EVERY earning or challenge-serving path is
  // gated (V1 per-miner pays into the live ledger; V2 is shadow). No side doors.
  const path = stripLegacyPrefix(url.pathname);
  if (method === "GET") {
    return path === "/v2/synthetic-boolean/per-miner/challenges"
      || path === "/v2/synthetic-boolean/per-miner/cnf"
      || path === "/v1/synthetic-boolean/per-miner/challenges"
      || path === "/v1/synthetic-boolean/per-miner/cnf"
      || path === "/v1/synthetic-boolean/active-cnf";
  }
  if (method === "POST") {
    return path === "/v2/agents/submit-bitset"
      || path === "/v2/agents/submit-manifest"
      || path === "/v2/blobs/solutions"
      || path === "/v1/agents/submit"
      || path === "/v1/external-scores/violet";
  }
  return false;
}

function stagedReopenResponse() {
  return new Response(JSON.stringify({
    detail: "v2_beta_staged_reopen",
    reason: "v2_beta_staged_reopen",
    message: "v2-beta is back online in staged mode. Full miner access is being restored via edge-published challenge artifacts. Retry later.",
    retry_after_seconds: 600,
  }), {
    status: 429,
    headers: {
      "content-type": "application/json",
      "retry-after": "600",
      "cache-control": "no-store",
      "x-cathedral-rejection-reason": "v2_beta_staged_reopen",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-staged-reopen",
    },
  });
}

function isPerMinerRead(url) {
  const path = stripLegacyPrefix(url.pathname);
  return path === "/v2/synthetic-boolean/per-miner/challenges"
    || path === "/v2/synthetic-boolean/per-miner/cnf"
    || path === "/v1/synthetic-boolean/per-miner/challenges"
    || path === "/v1/synthetic-boolean/per-miner/cnf";
}

function missingRequiredMinerHeaders(request) {
  return !request.headers.get("x-cathedral-hotkey")
    || !request.headers.get("x-cathedral-signature");
}

function edgeValidationResponse() {
  return new Response(JSON.stringify({
    detail: "missing required miner authentication headers",
    required_headers: ["x-cathedral-hotkey", "x-cathedral-signature"],
  }), {
    status: 422,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-cathedral-v2-beta-router": "cloudflare-worker",
      "x-cathedral-v2-beta-origin": "edge-preflight",
    },
  });
}

function forwardedHeaders(request) {
  const headers = new Headers(request.headers);
  for (const name of HOP_BY_HOP_HEADERS) headers.delete(name);
  headers.delete("host");
  headers.set("x-forwarded-host", new URL(request.url).host);
  headers.set("x-forwarded-proto", "https");
  headers.set("x-cathedral-v2-beta-router", "cloudflare-worker");
  return headers;
}

export default {
  async fetch(request) {
    const incomingUrl = new URL(request.url);
    if (request.method === "GET" && isPerMinerRead(incomingUrl) && missingRequiredMinerHeaders(request)) {
      return edgeValidationResponse();
    }
    if (isGatedMinerPath(incomingUrl, request.method)) {
      const hotkey = (request.headers.get("x-cathedral-hotkey") || "").trim();
      if (!CANARY_HOTKEYS.has(hotkey)) {
        return stagedReopenResponse();
      }
    }

    const originUrl = new URL(request.url);
    if (request.method === "GET" && isPerMinerRead(originUrl)) {
      const requestedLimit = Number(originUrl.searchParams.get("limit") || "0");
      if (!Number.isFinite(requestedLimit) || requestedLimit <= 0 || requestedLimit > 10) {
        originUrl.searchParams.set("limit", "10");
      }
    }
    originUrl.protocol = "http:";
    originUrl.hostname = ORIGIN_HOST;
    originUrl.port = ORIGIN_PORT;

    const init = {
      method: request.method,
      headers: forwardedHeaders(request),
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    const response = await fetch(originUrl, init);
    const headers = new Headers(response.headers);
    headers.set("x-cathedral-v2-beta-router", "cloudflare-worker");
    headers.set("x-cathedral-v2-beta-origin", "polaris-sandbox");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  },
};
