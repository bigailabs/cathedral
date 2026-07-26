"""Chain layer — subtensor connect, metagraph read, set_weights, and validator
provenance.

  * PROVENANCE. Every run records which repo/binary/commit is acting as the
    validator, on which hotkey, on which netuid, since when. The dashboard
    surfaces it so a tripartite vali is never confused with a production vali.

  * set_weights is DRY-RUN by default (compute + log the vector). This legacy
    scaffold can broadcast only away from SN39; the reviewed immutable
    ``cathedral-validator`` release is the sole SN39 writer.

bittensor is imported lazily; if it's absent (it isn't installed in every env)
the layer still computes weights and records provenance — it just can't read a
live metagraph or broadcast, which it reports honestly.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

CHAIN_ENDPOINT_ENV = "CATHEDRAL_CHAIN_ENDPOINT"
_announced = False


def connection_target(network: str) -> str:
    """Resolve WHERE TO CONNECT, decoupled from the network LABEL used for
    signing.

    If ``CATHEDRAL_CHAIN_ENDPOINT`` is set (e.g. ``wss://my-node:443``) we connect
    there; otherwise we fall back to the network label (``"finney"``), which
    bittensor resolves to a public entrypoint via ``NETWORK_MAP``. Either way the
    signed weight-vector keeps carrying the network *label* unchanged, so the
    orchestrator's signed-vector invariant (``network`` must equal the pinned
    value) is unaffected — this only redirects the RPC connection, e.g. to a
    self-hosted subtensor node. The endpoint must be a bittensor-compatible
    ws/wss URL on the SAME chain as the label (same genesis/UIDs)."""
    global _announced
    endpoint = os.environ.get(CHAIN_ENDPOINT_ENV, "").strip()
    if endpoint and not _announced:
        print(
            f"[chain] {CHAIN_ENDPOINT_ENV} active -> connecting to {endpoint} "
            f"(signing label stays {network!r})"
        )
        _announced = True
    return endpoint or network


@dataclass(frozen=True)
class ValiProvenance:
    """THE GUARD. Who is acting as the validator, right now."""

    repo: str
    commit: str
    validator_label: str  # e.g. "tripartite-vali" — NOT the prod vali
    hotkey: str  # on-chain hotkey used for submission
    netuid: int
    network: str  # finney | test | local
    started_at_iso: str
    broadcast_enabled: bool

    def banner(self) -> str:
        mode = "BROADCAST" if self.broadcast_enabled else "DRY-RUN (no broadcast)"
        return (
            f"[{self.validator_label}] repo={self.repo}@{self.commit} "
            f"hotkey={self.hotkey} netuid={self.netuid} net={self.network} "
            f"since={self.started_at_iso} mode={mode}"
        )


def _git_commit(repo_dir: str = ".") -> str:
    try:
        return (
            subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "?"
        )
    except Exception:
        return "?"


@dataclass
class WeightVector:
    """The validator's output for one round: uid -> normalized weight."""

    by_uid: dict[int, float] = field(default_factory=dict)
    by_label: dict[str, float] = field(default_factory=dict)  # human-readable

    def nonzero(self) -> dict[str, float]:
        return {
            k: v
            for k, v in sorted(self.by_label.items(), key=lambda kv: -kv[1])
            if v > 0
        }


class ChainClient:
    def __init__(
        self,
        *,
        netuid: int,
        network: str = "finney",
        wallet_name: str = "",
        hotkey: str = "",
        broadcast: bool = False,
    ):
        self.netuid = int(netuid)
        self.network = network
        self.wallet_name = wallet_name
        self.hotkey = hotkey
        self.broadcast = broadcast
        self._bt = None
        self._connect_error: str | None = None

    def _bittensor(self):
        if self._bt is None:
            try:
                import bittensor  # lazy; not installed everywhere

                self._bt = bittensor
            except Exception:
                self._bt = False
        return self._bt

    def _subtensor(self, bt):
        """Construct a subtensor across bittensor API variants (8.x bt.subtensor,
        10.x bt.Subtensor / core.subtensor.Subtensor). Connects to
        connection_target(self.network) — the CATHEDRAL_CHAIN_ENDPOINT override
        if set, else the network label resolved via NETWORK_MAP."""
        target = connection_target(self.network)
        self._connect_error = None
        last: Exception | None = None
        for ctor in ("subtensor", "Subtensor"):
            if hasattr(bt, ctor):
                try:
                    return getattr(bt, ctor)(network=target)
                except Exception as e:
                    last = e
        try:
            from bittensor.core.subtensor import Subtensor

            return Subtensor(network=target)
        except Exception as e:
            last = e
        # Surface the REAL failure (e.g. a wrong/unreachable CATHEDRAL_CHAIN_ENDPOINT
        # raises ConnectionRefused here) instead of a generic "no constructor".
        self._connect_error = f"could not connect to {target}: {last}"
        return None

    def read_metagraph(self) -> dict:
        """Read-only metagraph snapshot (uids, hotkeys, stake, validator_permit).
        Returns {'available': bool, ...}. Never writes. (Verified read-only
        2026-06-04 against finney: netuid 39 n=256, 12 permits, bittensor 10.3.2.)"""
        bt = self._bittensor()
        if not bt:
            return {
                "available": False,
                "reason": "bittensor not importable in this env",
            }
        try:
            sub = self._subtensor(bt)
            if sub is None:
                return {
                    "available": False,
                    "reason": self._connect_error
                    or "no compatible subtensor constructor",
                }
            mg = sub.metagraph(netuid=self.netuid)
            return {
                "available": True,
                "n": int(mg.n),
                "hotkeys": list(mg.hotkeys),
                "validator_permit": [bool(x) for x in mg.validator_permit],
                "stake": [float(x) for x in mg.S],
            }
        except Exception as e:
            return {"available": False, "reason": f"metagraph read failed: {e}"}

    def map_weights(
        self, label_scores: dict[str, float], meta: dict, our_uid: int | None = None
    ) -> WeightVector:
        """Map the validator's per-worker scores onto on-chain UIDs.

        Sim workers share ONE hotkey -> ONE UID, so on-chain the vector
        concentrates on our UID; the per-worker breakdown is kept for the
        dashboard (by_label). This is exactly why broadcasting sim grading to a
        real production metagraph is incoherent — recorded here, not hidden.
        """
        total = sum(label_scores.values())
        by_label = {k: round(v / (total or 1.0), 6) for k, v in label_scores.items()}
        by_uid: dict[int, float] = {}
        if our_uid is not None and total > 0:  # don't self-deal a vector of zeros
            by_uid[our_uid] = 1.0  # one shared HK == one UID
        return WeightVector(by_uid=by_uid, by_label=by_label)

    def set_weights(self, wv: WeightVector) -> dict:
        """Submit weights. DRY-RUN unless broadcast=True (explicit opt-in).

        SN39 is hard-disabled regardless of network label or endpoint. The
        immutable two-mode release is the only repository path permitted to
        write Cathedral mainnet weights.
        """
        if not self.broadcast:
            return {
                "submitted": False,
                "dry_run": True,
                "weight_vector": wv.nonzero(),
                "uids": wv.by_uid,
            }
        if self.netuid == 39:
            return {
                "submitted": False,
                "reason": (
                    "legacy scaffold broadcasts are disabled on SN39; use the "
                    "immutable cathedral-validator release"
                ),
            }
        bt = self._bittensor()
        if not bt:
            return {"submitted": False, "reason": "bittensor not importable"}
        try:
            sub = self._subtensor(bt)
            wallet = self._wallet(bt)
            if sub is None or wallet is None:
                return {
                    "submitted": False,
                    "reason": self._connect_error
                    or "no compatible subtensor/wallet ctor",
                }
            uids = list(wv.by_uid.keys())
            if not uids:
                return {
                    "submitted": False,
                    "reason": "empty weight vector (nothing to set)",
                }
            weights = [wv.by_uid[u] for u in uids]
            res = sub.set_weights(
                wallet=wallet,
                netuid=self.netuid,
                uids=uids,
                weights=weights,
                wait_for_inclusion=False,
            )
            ok, msg = res if isinstance(res, tuple) else (bool(res), "")
            return {"submitted": bool(ok), "msg": str(msg), "uids": uids}
        except Exception as e:
            return {"submitted": False, "reason": f"set_weights failed: {e}"}

    def _wallet(self, bt):
        """Construct a Wallet across bittensor API variants (10.x: bt.Wallet /
        bittensor_wallet.Wallet; 8.x: bt.wallet)."""
        for ctor in ("Wallet", "wallet"):
            if hasattr(bt, ctor):
                try:
                    return getattr(bt, ctor)(name=self.wallet_name, hotkey=self.hotkey)
                except Exception:
                    pass
        try:
            from bittensor_wallet import Wallet

            return Wallet(name=self.wallet_name, hotkey=self.hotkey)
        except Exception:
            return None


def make_provenance(
    *,
    validator_label: str,
    hotkey: str,
    netuid: int,
    network: str,
    started_at_iso: str,
    broadcast: bool,
    repo: str = "cathedral-scaffold",
) -> ValiProvenance:
    return ValiProvenance(
        repo=repo,
        commit=_git_commit(),
        validator_label=validator_label,
        hotkey=hotkey,
        netuid=netuid,
        network=network,
        started_at_iso=started_at_iso,
        broadcast_enabled=broadcast,
    )
