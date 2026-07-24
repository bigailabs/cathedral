"""The one supported candidate-snapshot capture command (round-four defect 6)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scaffold.snapshot_candidates import (
    SnapshotError,
    capture_candidate_snapshot,
    main,
    write_snapshot_atomic,
)

BLOCK_HASH = "0x" + "ab" * 32


class _FakeSubtensor:
    def __init__(
        self,
        *,
        hotkeys,
        block=100,
        block_hash=BLOCK_HASH,
        expected_block_arg=None,
    ):
        self._hotkeys = hotkeys
        self._block = block
        self._hash = block_hash
        self._expected_block_arg = expected_block_arg
        self.metagraph_calls: list[object] = []
        self.hash_calls: list[int] = []

    def metagraph(self, netuid, block=None):
        self.metagraph_calls.append((netuid, block))
        if self._expected_block_arg is not None:
            assert block == self._expected_block_arg
        # An honest chain returns the metagraph AT the requested block.
        returned_block = block if block is not None else self._block
        return SimpleNamespace(hotkeys=list(self._hotkeys), block=returned_block)

    def get_block_hash(self, block):
        self.hash_calls.append(block)
        return self._hash


def test_capture_produces_a_valid_sorted_snapshot():
    fake = _FakeSubtensor(hotkeys=["zed-hotkey", "alpha-hotkey"], block=123)
    document = capture_candidate_snapshot(
        network="finney",
        netuid=39,
        subtensor_factory=lambda network: fake,
    )
    assert document == {
        "schema": "cathedral_candidate_snapshot_v1",
        "network": "finney",
        "netuid": 39,
        "block": 123,
        "block_hash": BLOCK_HASH.lower(),
        "hotkeys": ["alpha-hotkey", "zed-hotkey"],
    }
    # The hash is queried for EXACTLY the captured block.
    assert fake.hash_calls == [123]

    # The confidential exporter accepts the captured document verbatim.
    from cathedral.score_class import validate_candidate_snapshot

    binding = validate_candidate_snapshot(document, network="finney", netuid=39)
    assert binding["block"] == 123
    assert binding["block_hash"] == "ab" * 32
    assert binding["hotkeys"] == ["alpha-hotkey", "zed-hotkey"]


def test_capture_pins_an_explicit_finalized_block():
    fake = _FakeSubtensor(hotkeys=["m1"], expected_block_arg=555)
    document = capture_candidate_snapshot(
        network="finney",
        netuid=39,
        block=555,
        subtensor_factory=lambda network: fake,
    )
    assert document["block"] == 555
    assert fake.metagraph_calls == [(39, 555)]
    assert fake.hash_calls == [555]


def test_capture_rejects_duplicate_and_malformed_chain_data():
    duplicate = _FakeSubtensor(hotkeys=["m1", "m1"])
    with pytest.raises(SnapshotError, match="duplicate hotkeys"):
        capture_candidate_snapshot(
            network="finney", netuid=39, subtensor_factory=lambda n: duplicate
        )
    malformed_hotkey = _FakeSubtensor(hotkeys=["m1", ""])
    with pytest.raises(SnapshotError, match="malformed hotkey"):
        capture_candidate_snapshot(
            network="finney", netuid=39, subtensor_factory=lambda n: malformed_hotkey
        )
    bad_hash = _FakeSubtensor(hotkeys=["m1"], block_hash="not-a-hash")
    with pytest.raises(SnapshotError, match="no usable hash"):
        capture_candidate_snapshot(
            network="finney", netuid=39, subtensor_factory=lambda n: bad_hash
        )
    no_block = _FakeSubtensor(hotkeys=["m1"], block=None)
    with pytest.raises(SnapshotError, match="usable block number"):
        capture_candidate_snapshot(
            network="finney", netuid=39, subtensor_factory=lambda n: no_block
        )
    with pytest.raises(SnapshotError, match="netuid is invalid"):
        capture_candidate_snapshot(
            network="finney", netuid=-1, subtensor_factory=lambda n: duplicate
        )


def test_write_snapshot_atomic_is_exact_and_replaces(tmp_path: Path):
    path = tmp_path / "snapshots" / "candidate-snapshot.json"
    document = {
        "schema": "cathedral_candidate_snapshot_v1",
        "network": "finney",
        "netuid": 39,
        "block": 100,
        "block_hash": BLOCK_HASH,
        "hotkeys": ["m1"],
    }
    write_snapshot_atomic(path, document)
    assert json.loads(path.read_text()) == document
    # No torn temp file survives, and a rewrite atomically replaces.
    assert not os.path.lexists(path.with_suffix(path.suffix + ".tmp"))
    write_snapshot_atomic(path, {**document, "block": 101})
    assert json.loads(path.read_text())["block"] == 101


def test_main_captures_via_cli(tmp_path: Path, monkeypatch, capsys):
    fake = _FakeSubtensor(hotkeys=["m2", "m1"], block=777, expected_block_arg=777)
    import scaffold.snapshot_candidates as module

    monkeypatch.setattr(module, "_default_subtensor_factory", lambda network: fake)
    output = tmp_path / "snap.json"
    code = main(
        [
            "--network",
            "finney",
            "--netuid",
            "39",
            "--block",
            "777",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["block"] == 777
    assert summary["hotkeys"] == 2
    assert json.loads(output.read_text())["hotkeys"] == ["m1", "m2"]


def test_main_reports_chain_failures_without_traceback(
    tmp_path: Path, monkeypatch, capsys
):
    import scaffold.snapshot_candidates as module

    def broken_factory(network):
        raise SnapshotError("chain unreachable")

    monkeypatch.setattr(module, "_default_subtensor_factory", broken_factory)
    code = main(
        ["--network", "finney", "--netuid", "39", "--output", str(tmp_path / "x.json")]
    )
    assert code == 2
    assert "candidate snapshot failed" in capsys.readouterr().err


def test_capture_refuses_a_metagraph_not_at_the_requested_block():
    """Round-six S1: the returned metagraph must BE at the explicitly
    requested block; a chain answering with different (or absent) state is
    an unproven binding and refuses."""

    class _DishonestSubtensor(_FakeSubtensor):
        def metagraph(self, netuid, block=None):
            self.metagraph_calls.append((netuid, block))
            return SimpleNamespace(hotkeys=list(self._hotkeys), block=999)

    dishonest = _DishonestSubtensor(hotkeys=["m1"])
    with pytest.raises(SnapshotError, match="refusing the unproven binding"):
        capture_candidate_snapshot(
            network="finney",
            netuid=39,
            block=555,
            subtensor_factory=lambda n: dishonest,
        )

    class _BlocklessSubtensor(_FakeSubtensor):
        def metagraph(self, netuid, block=None):
            return SimpleNamespace(hotkeys=list(self._hotkeys), block=None)

    blockless = _BlocklessSubtensor(hotkeys=["m1"])
    with pytest.raises(SnapshotError, match="binding cannot be proven"):
        capture_candidate_snapshot(
            network="finney",
            netuid=39,
            block=555,
            subtensor_factory=lambda n: blockless,
        )
