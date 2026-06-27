"""Canonical Subtensor coinbase conservation oracle.

This is the first strict oracle for the verifiable-SAT publishing pipeline.  It
models the childkey emission split shape from run_coinbase.rs:

    parent      = floor(validating_emission * parent_factor / u64::MAX)
    burn_take   = floor(parent * CKBurn / u64::MAX)
    child_take  = floor(parent * child_take_u16 / u16::MAX)
    parent_left = parent_emission.saturating_sub(burn_take)
    parent_left = parent_left.saturating_sub(child_take)

The conservation invariant is:

    burn_take + child_take + parent_left <= parent_emission

The bug class appears when both takes are computed from the original parent
emission, then sequential saturating subtraction hides the over-deduction.

The encoder is width-parametric.  CI uses a small bounded analogue where the
u16/u64 rate denominators scale with the bit width; launch-sized challenges use
u64 parent and CKBurn rates plus u16 child-take rates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from ..dimacs import verify_witness
from ..verify import UnsatCheck, verify_unsat_cert


CANONICAL_INVARIANT_ID = "subtensor.run_coinbase.childkey_conservation.v1"
SCHEMA_VERSION = "cathedral.coinbase_sat_oracle.v1"
SOURCE_TARGET = "pallets/subtensor/src/coinbase/run_coinbase.rs:1027-1039"
SUBTENSOR_DEFAULT_MAX_CHILDKEY_TAKE_U16 = 11_796


@dataclass(frozen=True)
class CoinbaseChallenge:
    schema_version: str
    invariant_id: str
    width: int
    ckb_enabled: bool
    cnf_text: str
    decode_map: dict[str, Any]
    clause_source_map: dict[str, Any]
    provenance: dict[str, Any]

    @property
    def cnf_sha256(self) -> str:
        return _sha_text(self.cnf_text)

    @property
    def mapping_sha256(self) -> str:
        return _sha_obj(self.clause_source_map)

    @property
    def artifact_sha256(self) -> str:
        return _sha_obj(self.to_public_artifact())

    def to_public_artifact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invariant_id": self.invariant_id,
            "width": self.width,
            "ckb_enabled": self.ckb_enabled,
            "cnf_sha256": self.cnf_sha256,
            "decode_map": self.decode_map,
            "decode_map_sha256": _sha_obj(self.decode_map),
            "clause_source_map": self.clause_source_map,
            "clause_source_map_sha256": self.mapping_sha256,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class CoinbaseSatVerdict:
    ok: bool
    reason: str
    decoded: dict[str, int]
    observed: dict[str, int | bool]
    cnf_sha256: str
    artifact_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "decoded": self.decoded,
            "observed": self.observed,
            "cnf_sha256": self.cnf_sha256,
            "artifact_sha256": self.artifact_sha256,
        }


def build_coinbase_challenge(
    *,
    ckb_enabled: bool,
    width: int = 64,
    agent_image_digest: str = "",
    agent_id: str = "hermes-coinbase-encoder-v1",
    work_nonce: str = "",
) -> CoinbaseChallenge:
    """Emit a canonical SAT/UNSAT challenge for the childkey invariant.

    `ckb_enabled=True` means the encoder is allowed to search for a non-zero
    burn take.  `ckb_enabled=False` forces burn_take=0 and the same invariant
    becomes UNSAT.
    """
    if width < 2 or width > 64:
        raise ValueError("width_must_be_2_to_64")

    rate_width = min(16, width)
    u64_rate_denominator = (1 << width) - 1
    child_rate_denominator = (1 << rate_width) - 1
    max_child_take_rate = _scaled_child_take_cap(rate_width)
    b = _Cnf()
    validating_emission = b.bits("validating_emission", width)
    parent_factor = b.bits("parent_factor", width)
    ck_burn_rate = b.bits("ck_burn_rate", width)
    child_take_rate = b.bits("child_take_rate", rate_width)

    with b.section(
        "reachable_parent_emission",
        "parent_emission = floor(validating_emission * parent_factor / u64::MAX)",
    ):
        b.assert_lit(b.nonzero(validating_emission))
        b.assert_lit(b.nonzero(parent_factor))
        parent = b.floor_mul_ratio(
            validating_emission,
            parent_factor,
            u64_rate_denominator,
            width,
            "parent_emission",
        )
        b.assert_lit(b.nonzero(parent))

    with b.section("ckburn_gate", "CKBurn=0 forces ck_burn_rate=0; CKBurn>0 requires ck_burn_rate>0"):
        if ckb_enabled:
            b.assert_lit(b.nonzero(ck_burn_rate))
        else:
            for bit in ck_burn_rate:
                b.assert_lit(-bit)

    with b.section(
        "child_take_gate",
        "child_take_rate <= runtime MaxChildkeyTake default 11796/65535",
    ):
        b.assert_lit(b.ule(child_take_rate, b.const_bits(max_child_take_rate, rate_width)))

    with b.section(
        "derive_burn_take",
        "burn_take = floor(parent_emission * ck_burn_rate / u64::MAX)",
    ):
        burn = b.floor_mul_ratio(
            parent,
            ck_burn_rate,
            u64_rate_denominator,
            width,
            "burn_take",
        )

    with b.section(
        "derive_child_take",
        "child_take = floor(parent_emission * child_take_rate / u16::MAX)",
    ):
        child = b.floor_mul_ratio(
            parent,
            child_take_rate,
            child_rate_denominator,
            width,
            "child_take",
        )

    with b.section("saturating_sub_1", "after_burn = parent_emission.saturating_sub(burn_take)"):
        after_burn, underflow1 = b.saturating_sub(parent, burn, "after_burn")

    with b.section("saturating_sub_2", "parent_left = after_burn.saturating_sub(child_take)"):
        parent_left, underflow2 = b.saturating_sub(after_burn, child, "parent_left")

    with b.section(
        "conservation_violation",
        "assert burn_take + child_take + parent_left > parent_emission",
    ):
        total = b.add_bits(b.add_bits(burn, child, "sum_burn_child"), parent_left, "sum_total")
        parent_ext = parent + [b.false_lit()] * (len(total) - len(parent))
        b.assert_lit(b.ugt(total, parent_ext))

    decode_map = {
        "kind": "bit_projection",
        "endianness": "lsb0",
        "required_fields": [
            "validating_emission",
            "parent_factor",
            "ck_burn_rate",
            "child_take_rate",
            "parent_emission",
            "burn_take",
            "child_take",
        ],
        "fields": {
            "validating_emission": {"bits": validating_emission, "unsigned": True},
            "parent_factor": {"bits": parent_factor, "unsigned": True},
            "ck_burn_rate": {"bits": ck_burn_rate, "unsigned": True},
            "child_take_rate": {"bits": child_take_rate, "unsigned": True},
            "parent_emission": {"bits": parent, "unsigned": True},
            "burn_take": {"bits": burn, "unsigned": True},
            "child_take": {"bits": child, "unsigned": True},
        },
    }
    cnf_text = b.dimacs(
        comments={
            "schema": SCHEMA_VERSION,
            "invariant_id": CANONICAL_INVARIANT_ID,
            "source_target": SOURCE_TARGET,
            "width": str(width),
            "rate_width": str(rate_width),
            "max_child_take_rate": str(max_child_take_rate),
            "ckb_enabled": str(bool(ckb_enabled)).lower(),
        },
    )
    provenance = _provenance(
        cnf_text=cnf_text,
        decode_map=decode_map,
        clause_source_map=b.source_map(),
        width=width,
        ckb_enabled=ckb_enabled,
        agent_image_digest=agent_image_digest,
        agent_id=agent_id,
        work_nonce=work_nonce,
    )
    return CoinbaseChallenge(
        schema_version=SCHEMA_VERSION,
        invariant_id=CANONICAL_INVARIANT_ID,
        width=width,
        ckb_enabled=ckb_enabled,
        cnf_text=cnf_text,
        decode_map=decode_map,
        clause_source_map=b.source_map(),
        provenance=provenance,
    )


def verify_coinbase_sat_assignment(
    challenge: CoinbaseChallenge,
    assignment: list[int],
) -> CoinbaseSatVerdict:
    """Verify a SAT assignment against the CNF and the real split arithmetic."""
    if challenge.invariant_id != CANONICAL_INVARIANT_ID:
        return _sat_verdict(False, "non_canonical_invariant", challenge, {}, {})
    if not verify_witness(challenge.cnf_text, assignment):
        return _sat_verdict(False, "sat_assignment_failed_cnf", challenge, {}, {})
    try:
        decoded = decode_coinbase_assignment(challenge, assignment)
        observed = run_childkey_split(
            validating_emission=decoded["validating_emission"],
            parent_factor=decoded["parent_factor"],
            ck_burn_rate=decoded["ck_burn_rate"],
            child_take_rate=decoded["child_take_rate"],
            width=challenge.width,
        )
    except ValueError as exc:
        return _sat_verdict(False, str(exc), challenge, {}, {})
    for field in ("parent_emission", "burn_take", "child_take"):
        if decoded.get(field) != observed.get(field):
            return _sat_verdict(False, f"decoded_{field}_not_reachable", challenge, decoded, observed)
    if not bool(observed["violation"]):
        return _sat_verdict(False, "real_replay_did_not_violate", challenge, decoded, observed)
    if not challenge.ckb_enabled:
        return _sat_verdict(False, "ckburn_disabled_but_sat_witness_found", challenge, decoded, observed)
    return _sat_verdict(True, "accepted_reproducible_coinbase_violation", challenge, decoded, observed)


def verify_coinbase_unsat_proof(
    challenge: CoinbaseChallenge,
    drat_text: str,
) -> UnsatCheck:
    """Verify the UNSAT side of the oracle with drat-trim."""
    if challenge.invariant_id != CANONICAL_INVARIANT_ID:
        return UnsatCheck(False, False, "non_canonical_invariant")
    if challenge.ckb_enabled:
        return UnsatCheck(False, False, "unsat_proof_for_sat_side")
    return verify_unsat_cert(challenge.cnf_text, drat_text)


def decode_coinbase_assignment(
    challenge: CoinbaseChallenge,
    assignment: list[int],
) -> dict[str, int]:
    values: dict[int, bool] = {}
    for lit in assignment:
        var = abs(int(lit))
        if var == 0:
            continue
        values[var] = lit > 0

    out: dict[str, int] = {}
    fields = challenge.decode_map.get("fields", {})
    for name, spec in fields.items():
        bits = spec.get("bits")
        if not isinstance(bits, list) or not bits:
            raise ValueError(f"decode_bits_missing:{name}")
        value = 0
        for idx, var in enumerate(bits):
            var = int(var)
            if var not in values:
                raise ValueError(f"decode_var_missing:{name}:{var}")
            if values[var]:
                value |= 1 << idx
        out[str(name)] = value
    return out


def run_childkey_split(
    *,
    validating_emission: int,
    parent_factor: int,
    ck_burn_rate: int,
    child_take_rate: int,
    width: int = 64,
) -> dict[str, int | bool]:
    """Pure replay of the bug-bearing run_coinbase childkey split arithmetic."""
    max_value = (1 << width) - 1
    rate_width = min(16, width)
    child_rate_max = (1 << rate_width) - 1
    max_child_take_rate = _scaled_child_take_cap(rate_width)
    for name, value in {
        "validating_emission": validating_emission,
        "parent_factor": parent_factor,
        "ck_burn_rate": ck_burn_rate,
    }.items():
        if not isinstance(value, int) or value < 0 or value > max_value:
            raise ValueError(f"{name}_outside_u{width}")
    if not isinstance(child_take_rate, int) or child_take_rate < 0 or child_take_rate > child_rate_max:
        raise ValueError(f"child_take_rate_outside_u{rate_width}")
    if child_take_rate > max_child_take_rate:
        raise ValueError("child_take_rate_above_subtensor_max")
    parent_emission = (validating_emission * parent_factor) // max_value
    burn_take = (parent_emission * ck_burn_rate) // max_value
    child_take = (parent_emission * child_take_rate) // child_rate_max
    after_burn = _sat_sub(parent_emission, burn_take)
    parent_left = _sat_sub(after_burn, child_take)
    total_extracted = burn_take + child_take + parent_left
    excess = max(0, total_extracted - parent_emission)
    return {
        "validating_emission": validating_emission,
        "parent_factor": parent_factor,
        "ck_burn_rate": ck_burn_rate,
        "child_take_rate": child_take_rate,
        "parent_emission": parent_emission,
        "burn_take": burn_take,
        "child_take": child_take,
        "after_burn": after_burn,
        "parent_left": parent_left,
        "total_extracted": total_extracted,
        "excess": excess,
        "violation": excess > 0,
    }


def attestation_report_data(
    challenge: CoinbaseChallenge,
    *,
    miner_hotkey: str = "",
    solver_artifact_hash: str = "",
) -> str:
    """Return the report_data payload expected from the Hermes encoder TDX run.

    The 64-byte TDX report_data slot should bind:
      first 32 bytes:  sha256(agent image || nonce || miner || solver artifact)
      second 32 bytes: sha256(cnf || decode map || clause map || invariant id)
    """
    image_digest = str(challenge.provenance.get("agent_image_digest") or "")
    nonce = str(challenge.provenance.get("work_nonce") or "")
    lo_preimage = {
        "agent_image_digest": image_digest,
        "work_nonce": nonce,
        "miner_hotkey": miner_hotkey,
        "solver_artifact_hash": solver_artifact_hash,
    }
    lo = hashlib.sha256(
        json.dumps(lo_preimage, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    hi = bytes.fromhex(challenge.provenance["work_product_sha256"])
    return (lo + hi).hex()


def _sat_verdict(
    ok: bool,
    reason: str,
    challenge: CoinbaseChallenge,
    decoded: dict[str, int],
    observed: dict[str, int | bool],
) -> CoinbaseSatVerdict:
    return CoinbaseSatVerdict(
        ok=ok,
        reason=reason,
        decoded=decoded,
        observed=observed,
        cnf_sha256=challenge.cnf_sha256,
        artifact_sha256=challenge.artifact_sha256,
    )


def _sat_sub(a: int, b: int) -> int:
    return 0 if b > a else a - b


def _provenance(
    *,
    cnf_text: str,
    decode_map: dict[str, Any],
    clause_source_map: dict[str, Any],
    width: int,
    ckb_enabled: bool,
    agent_image_digest: str,
    agent_id: str,
    work_nonce: str,
) -> dict[str, Any]:
    work_product = {
        "cnf_sha256": _sha_text(cnf_text),
        "decode_map_sha256": _sha_obj(decode_map),
        "clause_source_map_sha256": _sha_obj(clause_source_map),
        "invariant_id": CANONICAL_INVARIANT_ID,
        "width": width,
        "rate_width": min(16, width),
        "max_child_take_rate": _scaled_child_take_cap(min(16, width)),
        "max_child_take_source": "runtime SubtensorInitialMaxChildKeyTake=11796",
        "ckb_enabled": ckb_enabled,
    }
    return {
        "agent_id": agent_id,
        "agent_kind": "hermes_encoder",
        "agent_image_digest": agent_image_digest,
        "work_nonce": work_nonce,
        "canonical_invariant": True,
        "source_target": SOURCE_TARGET,
        "arithmetic_model": "bounded fixed-point floor-multiply rates with saturating_sub",
        "rate_width": min(16, width),
        "max_child_take_rate": _scaled_child_take_cap(min(16, width)),
        "max_child_take_source": "runtime SubtensorInitialMaxChildKeyTake=11796",
        "work_product_sha256": _sha_obj(work_product),
        "work_product": work_product,
        "tdx_report_data_policy": (
            "report_data = sha256(image_digest/nonce/miner/solver_artifact) || "
            "sha256(cnf/decode/map/invariant bundle)"
        ),
    }


def _scaled_child_take_cap(rate_width: int) -> int:
    if rate_width < 1 or rate_width > 16:
        raise ValueError("rate_width_must_be_1_to_16")
    denominator = (1 << rate_width) - 1
    return (SUBTENSOR_DEFAULT_MAX_CHILDKEY_TAKE_U16 * denominator) // 65_535


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_obj(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class _Cnf:
    def __init__(self) -> None:
        self.next_var = 1
        self.clauses: list[list[int]] = []
        self.names: dict[int, str] = {}
        self.sections: list[dict[str, Any]] = []
        self._true = self.var("const.true")
        self.add_clause([self._true])
        self._false = self.var("const.false")
        self.add_clause([-self._false])
        self._active_section: tuple[str, str, int] | None = None

    def var(self, name: str) -> int:
        v = self.next_var
        self.next_var += 1
        self.names[v] = name
        return v

    def bits(self, name: str, width: int) -> list[int]:
        return [self.var(f"{name}.{idx}") for idx in range(width)]

    def const_bits(self, value: int, width: int) -> list[int]:
        return [self.true_lit() if (value >> idx) & 1 else self.false_lit() for idx in range(width)]

    def true_lit(self) -> int:
        return self._true

    def false_lit(self) -> int:
        return self._false

    def add_clause(self, lits: list[int]) -> None:
        if not lits:
            raise ValueError("empty_clause_must_be_encoded_as_assertion_failure")
        self.clauses.append([int(lit) for lit in lits])

    def assert_lit(self, lit: int) -> None:
        self.add_clause([lit])

    def section(self, name: str, source: str) -> "_Section":
        return _Section(self, name, source)

    def _begin(self, name: str, source: str) -> None:
        self._active_section = (name, source, len(self.clauses) + 1)

    def _end(self) -> None:
        if self._active_section is None:
            return
        name, source, start = self._active_section
        end = len(self.clauses)
        self.sections.append({
            "name": name,
            "source": source,
            "dimacs_clause_start": start,
            "dimacs_clause_end": end,
        })
        self._active_section = None

    def source_map(self) -> dict[str, Any]:
        return {
            "schema": "cathedral.clause_source_map.v1",
            "source_target": SOURCE_TARGET,
            "sections": list(self.sections),
            "variables": {str(k): v for k, v in sorted(self.names.items())},
        }

    def dimacs(self, *, comments: dict[str, str]) -> str:
        lines: list[str] = []
        for key, value in comments.items():
            lines.append(f"c {key}: {value}")
        for var, name in sorted(self.names.items()):
            lines.append(f"c var {var}: {name}")
        for section in self.sections:
            lines.append(
                "c map "
                f"{section['dimacs_clause_start']}-{section['dimacs_clause_end']}: "
                f"{section['name']} | {section['source']}"
            )
        lines.append(f"p cnf {self.next_var - 1} {len(self.clauses)}")
        lines.extend(" ".join(str(lit) for lit in clause) + " 0" for clause in self.clauses)
        return "\n".join(lines) + "\n"

    def and2(self, a: int, b: int, name: str = "and") -> int:
        y = self.var(name)
        self.add_clause([-y, a])
        self.add_clause([-y, b])
        self.add_clause([-a, -b, y])
        return y

    def or2(self, a: int, b: int, name: str = "or") -> int:
        y = self.var(name)
        self.add_clause([-a, y])
        self.add_clause([-b, y])
        self.add_clause([a, b, -y])
        return y

    def nonzero(self, bits: list[int]) -> int:
        if not bits:
            return self.false_lit()
        out = bits[0]
        for idx, bit in enumerate(bits[1:], start=1):
            out = self.or2(out, bit, f"nonzero.{idx}")
        return out

    def zero_extend(self, bits: list[int], width: int) -> list[int]:
        if len(bits) > width:
            return bits[:width]
        return bits + [self.false_lit()] * (width - len(bits))

    def xor2(self, a: int, b: int, name: str = "xor") -> int:
        y = self.var(name)
        self.add_clause([-a, -b, -y])
        self.add_clause([a, b, -y])
        self.add_clause([a, -b, y])
        self.add_clause([-a, b, y])
        return y

    def xnor2(self, a: int, b: int, name: str = "xnor") -> int:
        return -self.xor2(a, b, name)

    def add_bits(self, a: list[int], b: list[int], name: str) -> list[int]:
        width = max(len(a), len(b))
        aa = a + [self.false_lit()] * (width - len(a))
        bb = b + [self.false_lit()] * (width - len(b))
        carry = self.false_lit()
        out: list[int] = []
        for idx, (abit, bbit) in enumerate(zip(aa, bb)):
            axb = self.xor2(abit, bbit, f"{name}.xor_ab.{idx}")
            out.append(self.xor2(axb, carry, f"{name}.sum.{idx}"))
            ab = self.and2(abit, bbit, f"{name}.carry_ab.{idx}")
            ac = self.and2(abit, carry, f"{name}.carry_ac.{idx}")
            bc = self.and2(bbit, carry, f"{name}.carry_bc.{idx}")
            carry = self.or2(self.or2(ab, ac, f"{name}.carry_or1.{idx}"), bc, f"{name}.carry.{idx}")
        out.append(carry)
        return out

    def mul_bits(self, a: list[int], b: list[int], name: str) -> list[int]:
        acc = [self.false_lit()] * (len(a) + len(b))
        for b_idx, b_bit in enumerate(b):
            row = [self.false_lit()] * b_idx
            row.extend(self.and2(a_bit, b_bit, f"{name}.row{b_idx}.{a_idx}") for a_idx, a_bit in enumerate(a))
            row = self.zero_extend(row, len(acc))
            acc = self.add_bits(acc, row, f"{name}.acc{b_idx}")
        return acc

    def mul_const_bits(self, bits: list[int], value: int, name: str) -> list[int]:
        if value < 0:
            raise ValueError("mul_const_negative")
        if value == 0:
            return [self.false_lit()]
        acc = [self.false_lit()]
        idx = 0
        v = value
        while v:
            if v & 1:
                row = [self.false_lit()] * idx + list(bits)
                width = max(len(acc), len(row))
                acc = self.add_bits(self.zero_extend(acc, width), self.zero_extend(row, width), f"{name}.const_acc{idx}")
            idx += 1
            v >>= 1
        return acc

    def floor_mul_ratio(
        self,
        value_bits: list[int],
        rate_bits: list[int],
        denominator: int,
        out_width: int,
        name: str,
    ) -> list[int]:
        if denominator <= 0:
            raise ValueError("ratio_denominator_must_be_positive")
        product = self.mul_bits(value_bits, rate_bits, f"{name}.product")
        quotient = self.bits(name, out_width)
        q_times_den = self.mul_const_bits(quotient, denominator, f"{name}.q_den")
        quotient_ext = quotient + [self.false_lit()]
        q_plus_one = self.add_bits(
            quotient_ext,
            self.const_bits(1, len(quotient_ext)),
            f"{name}.q_plus_one",
        )
        q1_times_den = self.mul_const_bits(q_plus_one, denominator, f"{name}.q1_den")
        cmp_width = max(len(product), len(q_times_den), len(q1_times_den))
        product_cmp = self.zero_extend(product, cmp_width)
        qd_cmp = self.zero_extend(q_times_den, cmp_width)
        q1d_cmp = self.zero_extend(q1_times_den, cmp_width)
        self.assert_lit(self.ule(qd_cmp, product_cmp))
        self.assert_lit(self.ult(product_cmp, q1d_cmp))
        return quotient

    def saturating_sub(self, a: list[int], b: list[int], name: str) -> tuple[list[int], int]:
        if len(a) != len(b):
            raise ValueError("saturating_sub_width_mismatch")
        borrow = self.false_lit()
        diff: list[int] = []
        for idx, (abit, bbit) in enumerate(zip(a, b)):
            axb = self.xor2(abit, bbit, f"{name}.diff_xor_ab.{idx}")
            diff.append(self.xor2(axb, borrow, f"{name}.diff.{idx}"))
            bor_b = self.or2(bbit, borrow, f"{name}.borrow_b_or_in.{idx}")
            term1 = self.and2(-abit, bor_b, f"{name}.borrow_term1.{idx}")
            term2 = self.and2(bbit, borrow, f"{name}.borrow_term2.{idx}")
            borrow = self.or2(term1, term2, f"{name}.borrow.{idx}")
        underflow = borrow
        out = [self.and2(bit, -underflow, f"{name}.sat_result.{idx}") for idx, bit in enumerate(diff)]
        return out, underflow

    def ule(self, a: list[int], b: list[int]) -> int:
        if len(a) != len(b):
            raise ValueError("compare_width_mismatch")
        lt = self.false_lit()
        eq = self.true_lit()
        for idx in reversed(range(len(a))):
            a_lt_b_here = self.and2(eq, self.and2(-a[idx], b[idx], f"lt.bit.{idx}"), f"lt.eq_and_bit.{idx}")
            lt = self.or2(lt, a_lt_b_here, f"lt.acc.{idx}")
            eq = self.and2(eq, self.xnor2(a[idx], b[idx], f"eq.bit.{idx}"), f"eq.acc.{idx}")
        return self.or2(lt, eq, "ule")

    def ugt(self, a: list[int], b: list[int]) -> int:
        if len(a) != len(b):
            raise ValueError("compare_width_mismatch")
        return self.ult(b, a)

    def ult(self, a: list[int], b: list[int]) -> int:
        if len(a) != len(b):
            raise ValueError("compare_width_mismatch")
        lt = self.false_lit()
        eq = self.true_lit()
        for idx in reversed(range(len(a))):
            a_lt_b_here = self.and2(eq, self.and2(-a[idx], b[idx], f"ult.bit.{idx}"), f"ult.eq_and_bit.{idx}")
            lt = self.or2(lt, a_lt_b_here, f"ult.acc.{idx}")
            eq = self.and2(eq, self.xnor2(a[idx], b[idx], f"ult.eq.bit.{idx}"), f"ult.eq.acc.{idx}")
        return lt


class _Section:
    def __init__(self, cnf: _Cnf, name: str, source: str) -> None:
        self.cnf = cnf
        self.name = name
        self.source = source

    def __enter__(self) -> None:
        self.cnf._begin(self.name, self.source)

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cnf._end()
