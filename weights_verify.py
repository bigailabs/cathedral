"""Focused smoke tests for scaffold.publisher.weights.

Run:
  python weights_verify.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from scaffold.publisher import weights
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher.store import Store


checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def row(score: Any) -> str:
    return json.dumps({"weighted_score": score})


def insert_eval(
    store: Store,
    row_id: str,
    ran_at: str,
    hotkey: str,
    row_json: str,
    *,
    task_type: str = "synthetic_boolean_v1",
    attested: bool = True,
) -> None:
    def _do(conn):
        conn.execute(
            "INSERT INTO eval_runs "
            "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json, attested) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, ran_at, 6, hotkey, task_type, row_json, 1 if attested else 0),
        )

    store.write(_do)


def update_row_json(store: Store, row_id: str, row_json: str) -> None:
    def _do(conn):
        conn.execute("UPDATE eval_runs SET row_json=? WHERE id=?", (row_json, row_id))

    store.write(_do)


def insert_solve(store: Store, challenge_id: str, hotkey: str, solved_at: str) -> None:
    def _do(conn):
        conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_solves "
            "(challenge_id, miner_hotkey, solved_at_iso) VALUES (?, ?, ?)",
            (challenge_id, hotkey, solved_at),
        )

    store.write(_do)


def insert_perminer_solve(
    store: Store,
    challenge_id: str,
    hotkey: str,
    epoch: int,
    *,
    tier: int = 1,
    seq: int = 0,
    difficulty_weight: float = 1.0,
    solved_at: str | None = None,
) -> None:
    def _do(conn):
        conn.execute(
            "INSERT OR IGNORE INTO per_miner_solves "
            "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, verified, solved_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                challenge_id,
                hotkey,
                epoch,
                tier,
                seq,
                difficulty_weight,
                solved_at or iso(datetime.now(timezone.utc)),
            ),
        )

    store.write(_do)


saved_env = {
    key: os.environ.get(key)
    for key in (
        "DATABASE_URL",
        weights.MODE_ENV,
        weights.ROW_SCORE_TASK_TYPES_ENV,
        weights.TIER_WEIGHTS_ENV,
        weights.TIER2_MULT_ENV,
        "CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE",
        "CATHEDRAL_PERMINER_ENABLED",
        "CATHEDRAL_PERMINER_SHADOW",
        weights.PERMINER_SCORING_MODE_ENV,
        weights.PERMINER_PUBLIC_BASELINE_ENV,
        weights.PERMINER_REQUIRE_COLDKEY_ENV,
    )
}
for key in saved_env:
    os.environ.pop(key, None)

store = Store(":memory:")
try:
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    recent = iso(now - timedelta(minutes=5))
    old = iso(now - timedelta(hours=30))

    insert_eval(store, "alpha-1", recent, "5Alpha", row(1.0))
    insert_eval(store, "alpha-2", recent, "5Alpha", row("1.0"))
    insert_eval(store, "beta-1", recent, "5Beta", row(1.0))
    insert_eval(store, "zero", recent, "5Zero", row(0.0))
    insert_eval(store, "negative", recent, "5Negative", row(-3.0))
    insert_eval(store, "nan", recent, "5NaN", row("NaN"))
    insert_eval(store, "malformed", recent, "5Malformed", "{not-json")
    insert_eval(store, "old-high", old, "5Old", row(999.0))
    insert_eval(
        store,
        "manual-unapproved",
        recent,
        "5Manual",
        row(999.0),
        task_type="manual_admin_override",
    )
    insert_eval(
        store,
        "unattested-high",
        recent,
        "5Unattested",
        row(999.0),
        attested=False,
    )

    ck("default mode is proportional", weights.mode() == "proportional")
    before_upgrade = weights.compose_scores(store, now=now)
    ck(
        "default proportional falls back to flat feed when solve ledger is empty",
        before_upgrade.get("5Alpha") == 1.0 and before_upgrade.get("5Beta") == 1.0,
    )

    update_row_json(store, "beta-1", row(4.0))
    after_upgrade_default = weights.compose_scores(store, now=now)
    ck(
        "default proportional fallback still ignores upgraded row_json weighted_score",
        after_upgrade_default.get("5Alpha") == 1.0
        and after_upgrade_default.get("5Beta") == 1.0,
    )

    os.environ[weights.MODE_ENV] = "row_score_recent"
    row_scores = weights.compose_scores(store, now=now)
    ck(
        "row_score_recent aggregates row_json weighted_score and normalizes to top miner",
        row_scores == {"5Alpha": 0.5, "5Beta": 1.0},
    )
    ck(
        "row_score_recent ignores malformed, old, zero, negative, and non-finite scores",
        not ({"5Malformed", "5Old", "5Zero", "5Negative", "5NaN"} & set(row_scores)),
    )
    ck("row_score_recent ignores unapproved task types",
       "5Manual" not in row_scores)
    ck("row_score_recent ignores unattested rows",
       "5Unattested" not in row_scores)
    os.environ["CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE"] = "1"
    collapsed_scores = weights.compose_scores(
        store,
        now=now,
        coldkey_of={"5Alpha": "5Cold", "5Beta": "5Cold"},
    )
    ck(
        "row_score_recent applies coldkey collapse when configured",
        collapsed_scores == {"5Alpha": 0.5, "5Beta": 0.5},
    )
    os.environ.pop("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", None)

    from scaffold.publisher import per_miner
    pm_store = Store(":memory:")
    try:
        pm_epoch = per_miner.current_epoch()
        insert_perminer_solve(pm_store, "pm-t1-a", "5PmAlpha", pm_epoch, seq=1)
        insert_perminer_solve(pm_store, "pm-t1-b", "5PmBeta", pm_epoch, seq=2)
        insert_perminer_solve(pm_store, "pm-t1-b", "5PmAlpha2", pm_epoch, seq=3)
        insert_perminer_solve(pm_store, "pm-t1-c", "5PmGamma", pm_epoch, seq=4)
        os.environ["CATHEDRAL_PERMINER_ENABLED"] = "1"
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ["CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE"] = "1"
        os.environ[weights.PERMINER_SCORING_MODE_ENV] = "assigned_only"
        pm_scores = weights.compose_scores(
            pm_store,
            now=now,
            coldkey_of={
                "5PmAlpha": "5PmCold",
                "5PmAlpha2": "5PmCold",
                "5PmBeta": "5PmCold",
                "5PmGamma": "5OtherCold",
            },
        )
        ck(
            "per-miner live scoring dedupes and splits one coldkey across stacked hotkeys",
            pm_scores == {
                "5PmAlpha": 0.333333,
                "5PmAlpha2": 0.333333,
                "5PmBeta": 0.333333,
                "5PmGamma": 1.0,
            },
        )
        def _map_pm(conn):
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PmAlpha", "5PmCold", recent),
            )
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PmAlpha2", "5PmCold", recent),
            )
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PmBeta", "5PmCold", recent),
            )
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PmGamma", "5OtherCold", recent),
            )
        pm_store.write(_map_pm)
        pm_explain = weights.explain_miner_score(pm_store, "5PmAlpha", now=now)
        ck(
            "miner explain uses same coldkey map as signed per-miner scoring",
            pm_explain["normalized_weight"] == pm_scores["5PmAlpha"],
        )
    finally:
        pm_store.close()
        os.environ.pop("CATHEDRAL_PERMINER_ENABLED", None)
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ.pop("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", None)
        os.environ.pop(weights.PERMINER_SCORING_MODE_ENV, None)

    pm_primary_store = Store(":memory:")
    try:
        from scaffold.publisher import per_miner

        pm_epoch = per_miner.current_epoch()
        os.environ[weights.MODE_ENV] = "proportional"
        os.environ["CATHEDRAL_PERMINER_ENABLED"] = "1"
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ[weights.PERMINER_SCORING_MODE_ENV] = "pm_primary"
        os.environ[weights.PERMINER_PUBLIC_BASELINE_ENV] = "0.05"
        insert_solve(pm_primary_store, "sat-t2-public-only", "5PublicOnly", recent)
        no_pm_scores = weights.compose_scores(pm_primary_store, now=now)
        ck(
            "pm_primary pays no public-board scores before accepted PM solves exist",
            no_pm_scores == {},
        )
        insert_perminer_solve(
            pm_primary_store,
            "pm-t2-private",
            "5Private",
            pm_epoch,
            tier=2,
            difficulty_weight=3.0,
            solved_at=recent,
        )
        insert_perminer_solve(
            pm_primary_store,
            "pm-t1-old-epoch-window",
            "5Private",
            pm_epoch - 1,
            tier=1,
            difficulty_weight=1.0,
            solved_at=iso(now - timedelta(hours=23)),
        )
        insert_perminer_solve(
            pm_primary_store,
            "pm-t2-too-old",
            "5Private",
            pm_epoch,
            tier=2,
            difficulty_weight=3.0,
            solved_at=iso(now - timedelta(hours=25)),
        )
        coldkey_blocked_scores = weights.compose_scores(pm_primary_store, now=now)
        ck(
            "pm_primary falls back to hotkey identity without coldkey map",
            coldkey_blocked_scores == {"5Private": 1.0},
        )
        coldkey_blocked_vec = weights.build_signed_vector(
            pm_primary_store,
            signing_key_hex=generate_test_key(),
            now=now,
        )
        ck(
            "pm_primary metadata stays active with hotkey fallback identity",
            coldkey_blocked_vec["policy_metadata"]["score_source"] == "pm_primary"
            and coldkey_blocked_vec["policy_metadata"]["perminer"]["primary_live"] is True
            and coldkey_blocked_vec["policy_metadata"]["perminer"]["identity_ready"] is True
            and coldkey_blocked_vec["policy_metadata"]["perminer"]["degraded_reason"] is None,
        )
        os.environ[weights.PERMINER_REQUIRE_COLDKEY_ENV] = "0"
        primary_scores = weights.compose_scores(pm_primary_store, now=now)
        ck(
            "pm_primary makes private assigned work the paying path",
            primary_scores == {"5Private": 1.0},
        )
        primary_vec = weights.build_signed_vector(
            pm_primary_store,
            signing_key_hex=generate_test_key(),
            now=now,
        )
        ck(
            "pm_primary signed vector exposes the live score source and baseline",
            primary_vec["policy_metadata"]["score_source"] == "pm_primary"
            and primary_vec["policy_metadata"]["perminer"]["primary_live"] is True
            and primary_vec["policy_metadata"]["perminer"]["public_baseline"] == 0.0,
        )
        primary_explain = weights.explain_miner_score(
            pm_primary_store,
            "5Private",
            now=now,
        )
        ck(
            "pm_primary miner explanation uses 24h PM units",
            primary_explain["score_source"] == "pm_primary"
            and primary_explain["raw_units"] == 4.0
            and primary_explain["top_units"] == 4.0
            and primary_explain["distinct_challenges"] == 2
            and primary_explain["normalized_weight"] == 1.0,
        )
        ck(
            "pm_primary explain exposes primary-live status",
            primary_explain["perminer"]["primary_live"] is True,
        )
    finally:
        pm_primary_store.close()
        os.environ.pop("CATHEDRAL_PERMINER_ENABLED", None)
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ.pop(weights.PERMINER_SCORING_MODE_ENV, None)
        os.environ.pop(weights.PERMINER_PUBLIC_BASELINE_ENV, None)
        os.environ.pop(weights.PERMINER_REQUIRE_COLDKEY_ENV, None)

    pm_mapped_store = Store(":memory:")
    try:
        from scaffold.publisher import per_miner

        pm_epoch = per_miner.current_epoch()
        os.environ[weights.MODE_ENV] = "proportional"
        os.environ["CATHEDRAL_PERMINER_ENABLED"] = "1"
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ[weights.PERMINER_SCORING_MODE_ENV] = "pm_primary"
        os.environ[weights.PERMINER_PUBLIC_BASELINE_ENV] = "0.05"
        os.environ[weights.PERMINER_REQUIRE_COLDKEY_ENV] = "1"
        insert_solve(pm_mapped_store, "sat-t2-public-only", "5PublicOnly", recent)
        insert_solve(pm_mapped_store, "sat-t2-unmapped-public", "5UnmappedPublic", recent)
        insert_perminer_solve(
            pm_mapped_store,
            "pm-t2-private-mapped",
            "5PrivateMapped",
            pm_epoch,
            tier=2,
            difficulty_weight=3.0,
            solved_at=recent,
        )
        insert_perminer_solve(
            pm_mapped_store,
            "pm-t2-unmapped-huge",
            "5UnmappedHuge",
            pm_epoch,
            tier=2,
            difficulty_weight=100.0,
            solved_at=recent,
        )

        def _map_private(conn):
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PrivateMapped", "5ColdPrivate", recent),
            )
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5PublicOnly", "5ColdPublic", recent),
            )

        pm_mapped_store.write(_map_private)
        mapped_scores = weights.compose_scores(
            pm_mapped_store,
            now=now,
            coldkey_of={"5PrivateMapped": "5ColdPrivate", "5PublicOnly": "5ColdPublic"},
        )
        ck(
            "pm_primary uses coldkey map opportunistically and falls back for unmapped PM hotkeys",
            mapped_scores.get("5UnmappedHuge") == 1.0
            and 0.02 < mapped_scores.get("5PrivateMapped", 0.0) < 0.04
            and "5PublicOnly" not in mapped_scores
            and "5UnmappedPublic" not in mapped_scores,
        )
        mapped_vec = weights.build_signed_vector(
            pm_mapped_store,
            signing_key_hex=generate_test_key(),
            now=now,
        )
        mapped_hotkeys = {w["miner_hotkey"] for w in mapped_vec["weights"]}
        ck(
            "pm_primary signed vector includes unmapped PM leaders via hotkey fallback",
            mapped_vec["policy_metadata"]["score_source"] == "pm_primary"
            and mapped_vec["policy_metadata"]["perminer"]["primary_live"] is True
            and "5UnmappedHuge" in mapped_hotkeys,
        )
        ck(
            "pm_primary signed vector excludes public-board-only hotkeys",
            "5UnmappedPublic" not in mapped_hotkeys,
        )
    finally:
        pm_mapped_store.close()
        os.environ.pop("CATHEDRAL_PERMINER_ENABLED", None)
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ.pop(weights.PERMINER_SCORING_MODE_ENV, None)
        os.environ.pop(weights.PERMINER_PUBLIC_BASELINE_ENV, None)
        os.environ.pop(weights.PERMINER_REQUIRE_COLDKEY_ENV, None)

    pm_budget_store = Store(":memory:")
    try:
        from scaffold.publisher import per_miner

        pm_epoch = per_miner.current_epoch()
        os.environ[weights.MODE_ENV] = "proportional"
        os.environ["CATHEDRAL_PERMINER_ENABLED"] = "1"
        os.environ[weights.PERMINER_SCORING_MODE_ENV] = "pm_primary"
        os.environ[weights.PERMINER_PUBLIC_BASELINE_ENV] = "0.02"
        os.environ[weights.PERMINER_REQUIRE_COLDKEY_ENV] = "1"
        insert_perminer_solve(
            pm_budget_store,
            "pm-t2-budget-private",
            "5BudgetPrivate",
            pm_epoch,
            tier=2,
            difficulty_weight=3.0,
            solved_at=recent,
        )
        for idx in range(60):
            hk = f"5BudgetPublic{idx:02d}"
            insert_solve(pm_budget_store, f"sat-t2-budget-public-{idx}", hk, recent)

        def _map_budget(conn):
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                ("5BudgetPrivate", "5ColdBudgetPrivate", recent),
            )
            for idx in range(60):
                hk = f"5BudgetPublic{idx:02d}"
                conn.execute(
                    "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                    "VALUES (?, ?, ?)",
                    (hk, f"5ColdBudgetPublic{idx:02d}", recent),
                )

        pm_budget_store.write(_map_budget)
        budget_scores = weights.compose_scores(
            pm_budget_store,
            now=now,
            coldkey_of={
                "5BudgetPrivate": "5ColdBudgetPrivate",
                **{f"5BudgetPublic{idx:02d}": f"5ColdBudgetPublic{idx:02d}" for idx in range(60)},
            },
        )
        public_total = round(sum(
            score for hk, score in budget_scores.items()
            if hk.startswith("5BudgetPublic")
        ), 6)
        ck(
            "pm_primary public baseline is disabled even with stale env",
            budget_scores.get("5BudgetPrivate") == 1.0 and public_total == 0.0,
        )
    finally:
        pm_budget_store.close()
        os.environ.pop("CATHEDRAL_PERMINER_ENABLED", None)
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ.pop(weights.PERMINER_SCORING_MODE_ENV, None)
        os.environ.pop(weights.PERMINER_PUBLIC_BASELINE_ENV, None)
        os.environ.pop(weights.PERMINER_REQUIRE_COLDKEY_ENV, None)

    ck("generic tier parser reads sat and future lane ids",
       weights.tier_from_challenge_id("sat-t2-abc") == 2
       and weights.tier_from_challenge_id("audit-t3-abc") == 3
       and weights.tier_from_challenge_id("pm-t4-e1-abc") == 4
       and weights.tier_from_challenge_id("client-t2-thing") == 1
       and weights.tier_from_challenge_id("tournament-77-t2-z") == 1
       and weights.tier_from_challenge_id("legacy") == 1)
    os.environ[weights.TIER2_MULT_ENV] = "5"
    os.environ.pop(weights.TIER_WEIGHTS_ENV, None)
    ck("tier weights default preserves tier2 multiplier",
       weights.tier_weights() == {1: 1.0, 2: 5.0})
    os.environ[weights.TIER_WEIGHTS_ENV] = "1=1,2=3,3=8"
    ck("tier weights parse comma launch config",
       weights.tier_weights() == {1: 1.0, 2: 3.0, 3: 8.0})
    os.environ[weights.TIER_WEIGHTS_ENV] = '{"1": 1, "2": 3, "3": 8, "4": 13}'
    ck("tier weights parse JSON launch config",
       weights.tier_weight(4) == 13.0 and weights.tier_weight(99) == 1.0)
    os.environ[weights.TIER_WEIGHTS_ENV] = "bad"
    ck("malformed tier weights fall back safely",
       weights.tier_weights() == {1: 1.0, 2: 5.0})

    os.environ[weights.MODE_ENV] = "proportional"
    os.environ[weights.TIER_WEIGHTS_ENV] = "1=1,2=3,3=8"
    prop_store = Store(":memory:")
    try:
        insert_solve(prop_store, "sat-t1-alpha", "5Alpha", recent)
        insert_solve(prop_store, "sat-t2-alpha", "5Alpha", recent)
        insert_solve(prop_store, "audit-t3-alpha", "5Alpha", recent)
        insert_solve(prop_store, "audit-t3-beta", "5Beta", recent)
        insert_solve(prop_store, "legacy-gamma", "5Gamma", recent)
        tier_scores = weights.compose_scores(prop_store, now=now)
        ck(
            "proportional mode uses explicit tier importance weights",
            tier_scores == {"5Alpha": 1.0, "5Beta": 0.666667, "5Gamma": 0.083333},
        )
        key_hex = generate_test_key()
        vec = weights.build_signed_vector(prop_store, signing_key_hex=key_hex, now=now)
        ck(
            "signed weight vector exposes tier-weight policy metadata",
            vec["policy_metadata"]["tier_weights"] == {1: 1.0, 2: 3.0, 3: 8.0}
            and vec["policy_metadata"]["effective_mode"] == "proportional"
            and vec["policy_metadata"]["score_source"] == "proportional",
        )
        explanation = weights.explain_miner_score(prop_store, "5Alpha", now=now)
        ck(
            "miner score explanation shows proportional tier-weighted units",
            explanation["score_source"] == "proportional"
            and explanation["raw_units"] == 12.0
            and explanation["top_units"] == 12.0
            and explanation["normalized_weight"] == 1.0
            and explanation["distinct_challenges"] == 3
            and explanation["tiers"] == [
                {"tier": 1, "solves": 1, "weighted_units": 1.0, "score_weight": 1.0},
                {"tier": 2, "solves": 1, "weighted_units": 3.0, "score_weight": 3.0},
                {"tier": 3, "solves": 1, "weighted_units": 8.0, "score_weight": 8.0},
            ],
        )
    finally:
        prop_store.close()

    empty_prop = Store(":memory:")
    try:
        insert_eval(empty_prop, "flat-fallback", recent, "5Flat", row(1.0))
        os.environ[weights.MODE_ENV] = "proportional"
        fallback_vec = weights.build_signed_vector(
            empty_prop,
            signing_key_hex=generate_test_key(),
            now=now,
        )
        ck(
            "proportional mode surfaces flat fallback when ledger is empty",
            fallback_vec["policy_metadata"]["requested_mode"] == "proportional"
            and fallback_vec["policy_metadata"]["effective_mode"] == "flat_recent_fallback"
            and fallback_vec["policy_metadata"]["score_source"] == "flat_recent_fallback"
            and fallback_vec["policy_metadata"]["proportional_ledger_empty"] is True
            and fallback_vec["policy_reason"].startswith("v4_flat_recent_fallback_"),
        )
    finally:
        empty_prop.close()

    key_hex = generate_test_key()
    os.environ[weights.MODE_ENV] = "row_score_recent"
    os.environ.pop(weights.TIER_WEIGHTS_ENV, None)
    vec = weights.build_signed_vector(store, signing_key_hex=key_hex, now=now)
    vector_scores = {w["miner_hotkey"]: w["weight"] for w in vec["weights"]}
    ck(
        "signed weight vector uses row_score_recent when explicitly enabled",
        vector_scores == {"5Alpha": 0.5, "5Beta": 1.0},
    )
    ck(
        "policy reason names the explicit row-score mode",
        vec["policy_reason"].startswith("v4_row_score_recent_"),
    )
finally:
    store.close()
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


failed = [name for name, ok in checks if not ok]
if failed:
    print("\nFAILED:")
    for name in failed:
        print(" -", name)
    sys.exit(1)
print(f"\nOK: {len(checks)} checks")
