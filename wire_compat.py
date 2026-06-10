"""Wire-compatibility gate: prove scaffold/wire.py is byte-identical to what
the 11 live SN39 validators verify — by checking REAL rows from the live feed
against the LIVE public key, using OUR canonicalization.

Offline by default (runs on the captured fixtures in fixtures/live-20260609/).
Pass --refresh to re-sample the live feed first.

If this gate passes, a publisher built on scaffold/wire.py can replace the
monolith with zero validator updates. If it ever fails after a wire change,
that change would have broken live validators — do not ship it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from scaffold.wire import SIGNED_KEYS_BY_VERSION, verify_row

FIX = Path(__file__).parent / "fixtures" / "live-20260609"
FEED = "https://api.cathedral.computer/v1/leaderboard/recent"

ok_n = 0
checks = []

def ck(name: str, cond: bool):
    checks.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")

if "--refresh" in sys.argv:
    import urllib.request
    with urllib.request.urlopen(f"{FEED}?since=2026-06-07T00:00:00Z&limit=50", timeout=30) as r:
        (FIX / "rows.json").write_bytes(r.read())
    with urllib.request.urlopen("https://api.cathedral.computer/.well-known/cathedral-jwks.json", timeout=30) as r:
        (FIX / "jwks.json").write_bytes(r.read())

jwks = json.loads((FIX / "jwks.json").read_text())
key = next(k for k in jwks["keys"] if k["kid"] == "cathedral-eval-signing")
pub_hex = key["public_key_hex"]
feed = json.loads((FIX / "rows.json").read_text())
rows = feed["items"]

print(f"wire-compat gate — {len(rows)} live rows, key {key['kid']} ({pub_hex[:12]}…)")

versions = sorted({int(r.get("eval_output_schema_version", 1)) for r in rows})
ck(f"fixture covers schema versions {versions}", set(versions) <= set(SIGNED_KEYS_BY_VERSION) and len(versions) >= 2)

bad = [r["id"] for r in rows if not verify_row(r, pub_hex)]
ck(f"all {len(rows)} live row signatures verify with OUR canonicalization", not bad)
if bad:
    print("    failing ids:", bad[:5])

per_v = {v: sum(1 for r in rows if int(r.get("eval_output_schema_version", 1)) == v) for v in versions}
for v, n in per_v.items():
    vrows = [r for r in rows if int(r.get("eval_output_schema_version", 1)) == v]
    ck(f"v{v} rows ({n}) all verify", all(verify_row(r, pub_hex) for r in vrows))

# tamper checks: any mutation of a signed field must break verification.
import copy
t = copy.deepcopy(rows[0]); t["weighted_score"] = 0.123
ck("tampered weighted_score breaks signature", not verify_row(t, pub_hex))
t = copy.deepcopy(rows[0]); t["miner_hotkey"] = "5" + "F" * 47
ck("tampered miner_hotkey breaks signature", not verify_row(t, pub_hex))
# post-signing fields are excluded — mutating them must NOT break verification.
t = copy.deepcopy(rows[0]); t["merkle_epoch"] = 999
ck("merkle_epoch is post-signing (mutation ignored)", verify_row(t, pub_hex))

ck("cursor contract present (next_since + tuple cursor)",
   all(k in feed for k in ("next_since", "next_since_ran_at", "next_since_id")))

fails = [n for n, c in checks if not c]
print(f"\nWIRE COMPAT: {'PASS ✅ all ' + str(len(checks)) + ' checks' if not fails else 'FAIL ❌ ' + str(fails)}")
sys.exit(1 if fails else 0)
