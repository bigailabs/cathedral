import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral_thin.core import ThinSubnetError
from cathedral_thin.report_cli import main as report_main
from cathedral_thin.score_classes import (
    DecisionStore,
    SourceCheckpoint,
    canonical_json,
    compose_class_decisions,
    decision_document,
    enforce_checkpoint,
    external_class_decision,
    load_best_report,
    load_score_policy,
    local_class_decision,
    sign_report,
    verify_report,
)


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def key_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode("ascii")


def write_policy(
    tmp_path, public_b64, *, asserted=False, locations=None, burn_hotkey=None
):
    assignment = {
        "mode": "asserted_score" if asserted else "metric",
        "metric": None if asserted else "verified_work_units",
        "transform": "linear",
        "cap": "100" if not asserted else "1",
        "required_reason_codes": ["receipt_verified"],
        "required_evidence_kinds": ["cathedral_assurance_receipt_v2"],
    }
    document = {
        "schema": "cathedral_score_policy_v1",
        "network": "finney",
        "netuid": 39,
        "classes": [
            {"allocation": "0.4", "class_id": "local_sat", "kind": "local_sat"},
            {
                "allocation": "0.6",
                "assignment": assignment,
                "class_id": "confidential_compute",
                "kind": "external",
                "locations": locations or [str(tmp_path / "report.json")],
                "max_age_seconds": 600,
                "max_block_span": 100,
                "max_future_seconds": 30,
                "require_evidence": True,
                "source_id": "cathedralconfidential",
                "trusted_keys": {"score-key-1": public_b64},
            },
        ],
    }
    if burn_hotkey is not None:
        document["burn_hotkey"] = burn_hotkey
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_json(document))
    return load_score_policy(path, network="finney", netuid=39)


def report_body(*, epoch=7, previous=None, entries=None):
    return {
        "schema": "cathedral_score_class_report_v1",
        "network": "finney",
        "netuid": 39,
        "class_id": "confidential_compute",
        "source_id": "cathedralconfidential",
        "source_epoch": epoch,
        "generated_at": "2026-07-18T12:00:00.000000Z",
        "valid_until": "2026-07-18T12:10:00.000000Z",
        "valid_from_block": 1000,
        "valid_until_block": 1050,
        "complete": True,
        "policy_digest": "sha256:" + "11" * 32,
        "verifier_digest": "sha256:" + "22" * 32,
        "previous_report_id": previous,
        "entries": entries
        or [
            {
                "miner_hotkey": "hotkey-a",
                "metrics": {"verified_work_units": "12.5"},
                "asserted_score": "0.99",
                "reason_codes": ["receipt_verified", "work_verified"],
                "evidence": [
                    {
                        "kind": "cathedral_assurance_receipt_v2",
                        "id": "receipt-sha256:" + "33" * 32,
                        "digest": "sha256:" + "44" * 32,
                        "uri": "https://mirror.example/receipts/33",
                    }
                ],
            },
            {
                "miner_hotkey": "hotkey-b",
                "metrics": {"verified_work_units": "6.25"},
                "asserted_score": "0.01",
                "reason_codes": ["receipt_verified"],
                "evidence": [
                    {
                        "kind": "cathedral_assurance_receipt_v2",
                        "id": "receipt-sha256:" + "55" * 32,
                        "digest": "sha256:" + "66" * 32,
                        "uri": None,
                    }
                ],
            },
        ],
        "signing_key_id": "score-key-1",
    }


def verified(tmp_path, *, epoch=7, previous=None, entries=None, asserted=False):
    private, public = key_material()
    policy = write_policy(tmp_path, public, asserted=asserted)
    raw = sign_report(
        report_body(epoch=epoch, previous=previous, entries=entries), private
    )
    report = verify_report(
        raw,
        policy.external_classes[0],
        network="finney",
        netuid=39,
        current_block=1010,
        now=NOW,
    )
    return policy, report, raw, private


def test_metric_assignment_is_validator_local_and_preserves_provenance(tmp_path):
    policy, report, _, _ = verified(tmp_path)
    decision = external_class_decision(
        policy.external_classes[0],
        report,
        coldkey_of={"hotkey-a": "cold-a", "hotkey-b": "cold-b"},
    )
    assert decision.raw_scores == {"hotkey-a": 12.5, "hotkey-b": 6.25}
    assert decision.normalized_weights == pytest.approx(
        {"hotkey-a": 2 / 3, "hotkey-b": 1 / 3}
    )
    assert decision.provenance["hotkey-a"]["evidence"][0]["id"].startswith(
        "receipt-sha256:"
    )


def test_asserted_score_mode_is_explicitly_distinct(tmp_path):
    policy, report, _, _ = verified(tmp_path, asserted=True)
    decision = external_class_decision(
        policy.external_classes[0],
        report,
        coldkey_of={"hotkey-a": "cold-a", "hotkey-b": "cold-b"},
    )
    assert decision.raw_scores == {"hotkey-a": 0.99, "hotkey-b": 0.01}


def test_tamper_wrong_domain_stale_block_and_network_fail_closed(tmp_path):
    policy, _, raw, _ = verified(tmp_path)
    external = policy.external_classes[0]
    tampered = json.loads(raw)
    tampered["entries"][0]["metrics"]["verified_work_units"] = "99"
    with pytest.raises(ThinSubnetError, match="id|signature"):
        verify_report(
            canonical_json(tampered),
            external,
            network="finney",
            netuid=39,
            current_block=1010,
            now=NOW,
        )
    with pytest.raises(ThinSubnetError, match="network"):
        verify_report(
            raw,
            external,
            network="test",
            netuid=39,
            current_block=1010,
            now=NOW,
        )
    with pytest.raises(ThinSubnetError, match="block window"):
        verify_report(
            raw,
            external,
            network="finney",
            netuid=39,
            current_block=2000,
            now=NOW,
        )
    with pytest.raises(ThinSubnetError, match="stale"):
        verify_report(
            raw,
            external,
            network="finney",
            netuid=39,
            current_block=1010,
            now=NOW + timedelta(hours=1),
        )

    private, public = key_material()
    bool_policy = write_policy(tmp_path, public)
    invalid_netuid = report_body()
    invalid_netuid["netuid"] = True
    with pytest.raises(ThinSubnetError, match="invalid"):
        verify_report(
            sign_report(invalid_netuid, private),
            bool_policy.external_classes[0],
            network="finney",
            netuid=True,
            current_block=1010,
            now=NOW,
        )


def test_duplicate_json_key_and_noncanonical_bytes_rejected(tmp_path):
    policy, _, raw, _ = verified(tmp_path)
    external = policy.external_classes[0]
    with pytest.raises(ThinSubnetError, match="canonical"):
        verify_report(
            raw + b"\n",
            external,
            network="finney",
            netuid=39,
            current_block=1010,
            now=NOW,
        )
    duplicate = raw.replace(b'"schema":', b'"schema":"bad","schema":', 1)
    with pytest.raises(ThinSubnetError, match="duplicate"):
        verify_report(
            duplicate,
            external,
            network="finney",
            netuid=39,
            current_block=1010,
            now=NOW,
        )


def test_checkpoint_blocks_rollback_equivocation_and_broken_chain(tmp_path):
    _, report, _, _ = verified(tmp_path, epoch=7)
    checkpoint = enforce_checkpoint(report, None)
    assert checkpoint == SourceCheckpoint(7, report.report_id)
    assert enforce_checkpoint(report, checkpoint) == checkpoint

    _, older, _, _ = verified(tmp_path, epoch=6)
    with pytest.raises(ThinSubnetError, match="rolled back"):
        enforce_checkpoint(older, checkpoint)

    changed_entries = report_body()["entries"]
    changed_entries[0]["metrics"]["verified_work_units"] = "13"
    _, equivocation, _, _ = verified(tmp_path, epoch=7, entries=changed_entries)
    with pytest.raises(ThinSubnetError, match="equivocated"):
        enforce_checkpoint(equivocation, checkpoint)

    _, next_report, _, _ = verified(tmp_path, epoch=8, previous="sha256:" + "aa" * 32)
    with pytest.raises(ThinSubnetError, match="does not extend"):
        enforce_checkpoint(next_report, checkpoint)


def test_mirrors_choose_highest_and_detect_same_epoch_equivocation(
    tmp_path, monkeypatch
):
    private, public = key_material()
    policy = write_policy(tmp_path, public, locations=["one", "two"])
    external = policy.external_classes[0]
    raw7 = sign_report(report_body(epoch=7), private)
    raw8 = sign_report(report_body(epoch=8), private)
    payloads = {"one": raw7, "two": raw8}
    monkeypatch.setattr(
        "cathedral_thin.score_classes.fetch_report", lambda location: payloads[location]
    )
    selected, checkpoint = load_best_report(
        external,
        network="finney",
        netuid=39,
        current_block=1010,
        checkpoint=None,
        now=NOW,
    )
    assert selected.source_epoch == 8
    assert checkpoint.source_epoch == 8

    other_key, _ = key_material()
    changed = report_body(epoch=8)
    changed["entries"][0]["metrics"]["verified_work_units"] = "13"
    # Same trusted key is required; use the original private key, not other_key.
    payloads["one"] = sign_report(changed, private)
    payloads["two"] = raw8
    with pytest.raises(ThinSubnetError, match="equivocation"):
        load_best_report(
            external,
            network="finney",
            netuid=39,
            current_block=1010,
            checkpoint=None,
            now=NOW,
        )
    del other_key


def test_class_composition_preserves_budgets_and_collapses_coldkeys(tmp_path):
    policy, report, _, _ = verified(tmp_path)
    coldkeys = {"hotkey-a": "same", "hotkey-b": "same", "hotkey-c": "other"}
    local = local_class_decision(
        policy.local_class,
        {"hotkey-a": 1.0, "hotkey-b": 1.0, "hotkey-c": 1.0},
        coldkey_of=coldkeys,
        reasons={key: "verified" for key in coldkeys},
    )
    external = external_class_decision(
        policy.external_classes[0], report, coldkey_of=coldkeys
    )
    final = compose_class_decisions(policy, [local, external])
    assert sum(final.values()) == pytest.approx(1.0)
    # Local class gives the shared coldkey one half of its 40% budget; external
    # class gives it all 60%. The two hotkeys cannot mint another coldkey budget.
    assert final["hotkey-a"] + final["hotkey-b"] == pytest.approx(0.8)
    assert final["hotkey-c"] == pytest.approx(0.2)


def test_empty_local_class_cannot_donate_its_budget_to_external_class(tmp_path):
    policy, report, _, _ = verified(tmp_path)
    coldkeys = {"hotkey-a": "cold-a", "hotkey-b": "cold-b"}
    local = local_class_decision(
        policy.local_class,
        {"hotkey-a": 0.0, "hotkey-b": 0.0},
        coldkey_of=coldkeys,
        reasons={"hotkey-a": "timeout", "hotkey-b": "witness_failed"},
    )
    assert local.normalized_weights == {}
    external = external_class_decision(
        policy.external_classes[0], report, coldkey_of=coldkeys
    )

    with pytest.raises(ThinSubnetError, match="class weights are not normalized"):
        compose_class_decisions(policy, [local, external])


def test_empty_class_allocation_moves_to_configured_burn_hotkey(tmp_path):
    _private, public = key_material()
    policy = write_policy(tmp_path, public, burn_hotkey="burn-hotkey")
    _, report, _, _ = verified(tmp_path)
    coldkeys = {"hotkey-a": "cold-a", "hotkey-b": "cold-b"}
    local = local_class_decision(
        policy.local_class,
        {"hotkey-a": 0.0, "hotkey-b": 0.0},
        coldkey_of=coldkeys,
        reasons={"hotkey-a": "timeout", "hotkey-b": "witness_failed"},
    )
    external = external_class_decision(
        policy.external_classes[0], report, coldkey_of=coldkeys
    )

    final = compose_class_decisions(policy, [local, external])
    assert final["burn-hotkey"] == pytest.approx(0.4)
    assert final["hotkey-a"] + final["hotkey-b"] == pytest.approx(0.6)
    assert sum(final.values()) == pytest.approx(1.0)


def test_positive_metric_requires_evidence_and_missing_class_holds(tmp_path):
    empty_evidence = report_body()["entries"]
    empty_evidence[0]["evidence"] = []
    policy, report, _, _ = verified(tmp_path, entries=empty_evidence)
    with pytest.raises(ThinSubnetError, match="lacks required evidence"):
        external_class_decision(
            policy.external_classes[0],
            report,
            coldkey_of={"hotkey-a": "a", "hotkey-b": "b"},
        )

    local = local_class_decision(
        policy.local_class,
        {"hotkey-a": 1},
        coldkey_of={"hotkey-a": "a"},
        reasons={"hotkey-a": "verified"},
    )
    with pytest.raises(ThinSubnetError, match="cover"):
        compose_class_decisions(policy, [local])
    with pytest.raises(ThinSubnetError, match="cover"):
        compose_class_decisions(policy, [local, local])

    wrong_reason = report_body()["entries"]
    wrong_reason[0]["reason_codes"] = ["work_verified"]
    policy, report, _, _ = verified(tmp_path, entries=wrong_reason)
    with pytest.raises(ThinSubnetError, match="required reasons"):
        external_class_decision(
            policy.external_classes[0],
            report,
            coldkey_of={"hotkey-a": "a", "hotkey-b": "b"},
        )

    wrong_kind = report_body()["entries"]
    wrong_kind[0]["evidence"][0]["kind"] = "other_receipt"
    policy, report, _, _ = verified(tmp_path, entries=wrong_kind)
    with pytest.raises(ThinSubnetError, match="required evidence kinds"):
        external_class_decision(
            policy.external_classes[0],
            report,
            coldkey_of={"hotkey-a": "a", "hotkey-b": "b"},
        )


def test_evidence_uri_cannot_smuggle_credentials_or_local_paths(tmp_path):
    for uri in ("file:///private/receipt.json", "https://token@example.test/r"):
        entries = report_body()["entries"]
        entries[0]["evidence"][0]["uri"] = uri
        with pytest.raises(ThinSubnetError, match="credential-free"):
            verified(tmp_path, entries=entries)


def test_decision_record_is_vector_bound_and_immutable(tmp_path):
    policy, report, _, _ = verified(tmp_path)
    external = external_class_decision(
        policy.external_classes[0],
        report,
        coldkey_of={"hotkey-a": "a", "hotkey-b": "b"},
    )
    local = local_class_decision(
        policy.local_class,
        {"hotkey-a": 1, "hotkey-b": 1},
        coldkey_of={"hotkey-a": "a", "hotkey-b": "b"},
        reasons={"hotkey-a": "verified", "hotkey-b": "verified"},
    )
    document, digest = decision_document(
        validator_hotkey="validator",
        network="finney",
        netuid=39,
        round_id=10,
        block=1010,
        policy_digest=policy.digest,
        decisions=[local, external],
        peers=[
            {"uid": 1, "hotkey": "hotkey-a", "coldkey": "a", "serviceable": True},
            {"uid": 2, "hotkey": "hotkey-b", "coldkey": "b", "serviceable": True},
        ],
        uids=[1, 2],
        weights=[0.65, 0.35],
    )
    store = DecisionStore(tmp_path / "decisions")
    path = store.write(document)
    assert path == store.write(document)
    assert json.loads(path.read_text())["decision_digest"] == digest
    changed = dict(document)
    changed["block"] = 1011
    with pytest.raises(ThinSubnetError, match="digest mismatch"):
        store.write(changed)
    with pytest.raises(ThinSubnetError, match="aligned"):
        decision_document(
            validator_hotkey="validator",
            network="finney",
            netuid=39,
            round_id=10,
            block=1010,
            policy_digest=policy.digest,
            decisions=[local, external],
            peers=[],
            uids=[1],
            weights=[],
        )


def test_policy_rejects_implicit_reallocation_and_noncanonical_config(tmp_path):
    _, public = key_material()
    policy = {
        "schema": "cathedral_score_policy_v1",
        "network": "finney",
        "netuid": 39,
        "classes": [
            {"allocation": "0.9", "class_id": "local_sat", "kind": "local_sat"}
        ],
    }
    path = tmp_path / "bad.json"
    path.write_bytes(canonical_json(policy))
    with pytest.raises(ThinSubnetError, match="sum exactly"):
        load_score_policy(path, network="finney", netuid=39)
    path.write_text(json.dumps(policy, indent=2))
    with pytest.raises(ThinSubnetError, match="canonical"):
        load_score_policy(path, network="finney", netuid=39)
    assert public


def test_producer_cli_signs_self_verifies_and_refuses_overwrite(tmp_path, capsys):
    private, _ = key_material()
    seed = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "score.seed"
    key_path.write_text(base64.b64encode(seed).decode("ascii") + "\n")
    key_path.chmod(0o600)
    body_path = tmp_path / "body.json"
    body_path.write_bytes(canonical_json(report_body()))
    output = tmp_path / "report.json"
    assert (
        report_main(
            [
                "sign",
                "--key-file",
                str(key_path),
                "--body",
                str(body_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "signed"
    assert output.exists()
    assert report_main(["public-key", "--key-file", str(key_path)]) == 0
    assert "public_key_base64" in json.loads(capsys.readouterr().out)

    changed = report_body(epoch=8)
    body_path.write_bytes(canonical_json(changed))
    assert (
        report_main(
            [
                "sign",
                "--key-file",
                str(key_path),
                "--body",
                str(body_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "refusing to overwrite" in capsys.readouterr().out
    assert (
        report_main(
            [
                "sign",
                "--key-file",
                str(key_path),
                "--body",
                str(body_path),
                "--output",
                str(output),
                "--replace-latest",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["source_epoch"] == 8
    capsys.readouterr()


def test_producer_cli_rejects_insecure_signing_key(tmp_path, capsys):
    private, _ = key_material()
    seed = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "score.seed"
    key_path.write_text(base64.b64encode(seed).decode("ascii"))
    key_path.chmod(0o644)
    assert report_main(["public-key", "--key-file", str(key_path)]) == 1
    assert "group/world" in capsys.readouterr().out
