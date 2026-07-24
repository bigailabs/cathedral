"""Small producer utility for immutable score-class reports."""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .core import ThinSubnetError
from .score_classes import (
    AssignmentPolicy,
    ExternalClassPolicy,
    canonical_json,
    parse_strict_json,
    parse_time,
    sign_report,
    verify_report,
)


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key_path = Path(path).expanduser()
    try:
        info = key_path.lstat()
        if key_path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ThinSubnetError(
                "score signing key must be a regular non-symlink file"
            )
        if info.st_mode & 0o077:
            raise ThinSubnetError(
                "score signing key must not be group/world accessible"
            )
        encoded = key_path.read_text(encoding="ascii")
    except ThinSubnetError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ThinSubnetError(f"could not read score signing key: {key_path}") from exc
    if encoded.endswith("\n"):
        encoded = encoded[:-1]
    try:
        seed = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ThinSubnetError("score signing key must be canonical base64") from exc
    if len(seed) != 32 or base64.b64encode(seed).decode("ascii") != encoded:
        raise ThinSubnetError("score signing seed must be exactly 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_base64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def _self_verify(raw: bytes, public_key: bytes) -> str:
    document = parse_strict_json(raw)
    first = document.get("valid_from_block")
    last = document.get("valid_until_block")
    generated = document.get("generated_at")
    if (
        isinstance(first, bool)
        or not isinstance(first, int)
        or isinstance(last, bool)
        or not isinstance(last, int)
        or not isinstance(generated, str)
    ):
        raise ThinSubnetError("unsigned report has invalid block or time fields")
    policy = ExternalClassPolicy(
        class_id=str(document.get("class_id", "")),
        allocation=Decimal(1),
        source_id=str(document.get("source_id", "")),
        locations=("self-check",),
        trusted_keys={str(document.get("signing_key_id", "")): public_key},
        max_age_seconds=1,
        max_future_seconds=1,
        max_block_span=max(1, last - first),
        require_evidence=False,
        assignment=AssignmentPolicy("asserted_score", None, "linear", None),
    )
    report = verify_report(
        raw,
        policy,
        network=str(document.get("network", "")),
        netuid=document.get("netuid"),
        current_block=first,
        now=parse_time(generated, "generated_at"),
    )
    return report.report_id


def write_report(path: str | Path, raw: bytes, *, replace_latest: bool = False) -> Path:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not replace_latest:
        if output.read_bytes() == raw:
            return output
        raise ThinSubnetError(f"refusing to overwrite a different report: {output}")
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if replace_latest:
            os.replace(tmp, output)
        else:
            try:
                os.link(tmp, output)
            except FileExistsError:
                if output.read_bytes() != raw:
                    raise ThinSubnetError(
                        f"refusing to overwrite a different report: {output}"
                    ) from None
    finally:
        if tmp.exists():
            tmp.unlink()
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sign or inspect a Cathedral score-class producer key"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    public = subparsers.add_parser("public-key")
    public.add_argument("--key-file", required=True)
    sign = subparsers.add_parser("sign")
    sign.add_argument("--key-file", required=True)
    sign.add_argument("--body", required=True, help="canonical unsigned report JSON")
    sign.add_argument("--output", required=True)
    sign.add_argument(
        "--replace-latest",
        action="store_true",
        help="atomically replace an explicitly mutable latest-report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        private_key = load_private_key(args.key_file)
        public_b64 = public_key_base64(private_key)
        if args.command == "public-key":
            print(json.dumps({"algorithm": "ed25519", "public_key_base64": public_b64}))
            return 0
        body_path = Path(args.body).expanduser()
        body_raw = body_path.read_bytes()
        document = parse_strict_json(body_raw)
        if body_raw != canonical_json(document):
            raise ThinSubnetError("unsigned report body must be canonical JSON")
        raw = sign_report(document, private_key)
        public_raw = base64.b64decode(public_b64, validate=True)
        report_id = _self_verify(raw, public_raw)
        output = write_report(
            args.output, raw, replace_latest=bool(args.replace_latest)
        )
        print(
            json.dumps(
                {
                    "output": str(output),
                    "public_key_base64": public_b64,
                    "report_id": report_id,
                    "status": "signed",
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ThinSubnetError) as exc:
        print(json.dumps({"error": str(exc), "status": "rejected"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
