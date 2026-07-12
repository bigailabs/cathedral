import assert from "node:assert/strict";
import test from "node:test";

import { validatePayload } from "./worker.mjs";

function vector(overrides = {}) {
  const weights = Array.from({ length: 244 }, (_, i) => ({
    miner_hotkey: `hotkey-${i}`,
    weight: 1,
  }));
  return {
    network: "finney",
    netuid: 39,
    key_id: "cathedral-weight-policy",
    signature: "signed",
    expires_at: "2999-01-01T00:00:00.000Z",
    weights,
    policy_metadata: {
      payable_hotkeys: {
        mode: "filter",
        enforced: true,
        snapshot_fresh: true,
        snapshot_hotkey_count: 256,
        final_miner_count: weights.length,
      },
    },
    ...overrides,
  };
}

test("accepts a registered-only vector below the obsolete 300-entry floor", () => {
  const payload = vector();
  assert.deepEqual(validatePayload(JSON.stringify(payload)), payload);
});

test("rejects a vector when payable filtering is disabled", () => {
  const payload = vector();
  payload.policy_metadata.payable_hotkeys.enforced = false;
  assert.throws(
    () => validatePayload(JSON.stringify(payload)),
    /payable_filter_not_enforced/,
  );
});

test("rejects stale or implausibly small metagraph snapshots", () => {
  const stale = vector();
  stale.policy_metadata.payable_hotkeys.snapshot_fresh = false;
  assert.throws(() => validatePayload(JSON.stringify(stale)), /bad_metagraph_snapshot/);

  const small = vector();
  small.policy_metadata.payable_hotkeys.snapshot_hotkey_count = 12;
  assert.throws(() => validatePayload(JSON.stringify(small)), /bad_metagraph_snapshot/);
});

test("rejects a mismatch between filtered metadata and vector length", () => {
  const payload = vector();
  payload.policy_metadata.payable_hotkeys.final_miner_count = 243;
  assert.throws(() => validatePayload(JSON.stringify(payload)), /filtered_count_mismatch/);
});
