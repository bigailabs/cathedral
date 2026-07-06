"""Once-per-epoch assignment publisher (edge-read architecture).

Instead of computing per-miner challenges on every poll (which pins the DB pool
and forces uncacheable origin reads), this job runs ONCE per epoch and publishes
each miner's full assignment set to a public Hippius S3 bucket:

  cnf/<sha>.cnf                     immutable CNF bodies, deduped by sha256
  epoch/<epoch>/<hotkey>.json       signed per-miner assignment manifest
  latest.json                       signed pointer {epoch, published_at}

Miners then read from Hippius (no auth, no origin). The origin only accepts
submits + runs verify. Everything the old challenges handler computed per item is
a pure function of (hotkey, epoch, tier, seq) + the token secret, so it moves
here unchanged.

This module is pure orchestration over per_miner + v2_bitset_submit + a blob
sink; it holds no wall-clock (timestamps are passed in) so it is testable and
resumable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Iterable

from . import per_miner as pm
from . import real_corpus
from . import v2_bitset_submit
from ..dimacs import parse_cnf

ASSIGNMENT_SCHEMA = "cathedral.v2.assignment.v1"
POINTER_SCHEMA = "cathedral.v2.epoch_pointer.v1"


def build_item(hotkey: str, epoch: int, tier: int, seq: int, *,
               token_secret: str, expires_at: str,
               not_before: str | None = None) -> tuple[dict[str, Any], str, bytes]:
    """Pure: build ONE assignment item + its CNF. Returns (item, cnf_sha, cnf_bytes).

    Mirrors the per-item loop in the old v2_per_miner_challenges handler exactly:
    generate, parse nvars, label kind, sha, mint token. The planted witness
    (generate_instance's 3rd return) is DELIBERATELY discarded here — it is never
    published, so a public bucket leaks nothing usable.

    not_before pins the earliest submit time (the epoch open) so a miner cannot
    pre-solve a manifest fetched early and submit before the epoch officially
    starts (Codex gate-1 blocker fix).
    """
    cid, cnf_text, planted = pm.generate_instance(hotkey, epoch, tier, seq)
    nvars, _clauses = parse_cnf(cnf_text)
    cnf_bytes = cnf_text.encode("utf-8")
    cnf_sha = hashlib.sha256(cnf_bytes).hexdigest()
    kind = (
        real_corpus.kind_for(epoch, tier, seq, salt=hotkey)
        if planted is None else "random_3sat_perminer"
    )
    token = v2_bitset_submit.mint_submit_token(
        secret=token_secret, miner_hotkey=hotkey, challenge_id=cid,
        epoch=epoch, tier=tier, seq=seq, nvars=nvars, cnf_sha256=cnf_sha,
        expires_at=expires_at, not_before=not_before,
    )
    item = {
        "challenge_id": cid, "tier": tier, "seq": seq, "n_vars": nvars,
        "cnf_sha256": cnf_sha, "cnf_key": f"cnf/{cnf_sha}.cnf",
        "kind": kind, "assignment_encoding": "bitset/v1",
        "submit_token": token, "submit_token_expires_at": expires_at,
    }
    return item, cnf_sha, cnf_bytes


def miner_assignment(hotkey: str, epoch: int, *, token_secret: str, expires_at: str,
                     allotment_by_tier: dict[int, int],
                     not_before: str | None = None) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Pure: full assignment for one miner this epoch + the CNF bytes to upload.

    Returns (items, {cnf_sha: cnf_bytes}). CNF bytes are deduped by sha across the
    miner's set (identical instances collapse to one upload).
    """
    items: list[dict[str, Any]] = []
    cnfs: dict[str, bytes] = {}
    for tier in sorted(allotment_by_tier):
        for seq in range(allotment_by_tier[tier]):
            item, sha, body = build_item(
                hotkey, epoch, tier, seq,
                token_secret=token_secret, expires_at=expires_at,
                not_before=not_before)
            items.append(item)
            cnfs[sha] = body
    return items, cnfs


def sign_manifest(manifest: dict[str, Any], sign: Callable[[bytes], str]) -> dict[str, Any]:
    """Attach an ed25519 publisher signature over the canonical manifest body.

    The signature is defense-in-depth: submit RE-verifies token + sha
    independently, so a tampered manifest cannot cause a bad score — but the
    signature lets a miner detect tampering before wasting solve time.
    """
    body = {k: manifest[k] for k in manifest if k != "publisher_sig"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signed = dict(manifest)
    signed["publisher_sig"] = sign(canonical)
    return signed


def publish_epoch(
    *,
    epoch: int,
    hotkeys: Iterable[str],
    token_secret: str,
    expires_at: str,
    published_at: str,
    reveal_nonce: str,
    not_before: str | None = None,
    allotment_by_tier: dict[int, int],
    cnf_base_url: str,
    put_object: Callable[[str, bytes, str], str],
    sign: Callable[[bytes], str],
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Orchestrate a full epoch publish with an ATOMIC REVEAL. Side effects are
    ALL via put_object, so this is testable with an in-memory sink.

    Atomic reveal (Codex gate-1 blocker fix): per-miner manifests are written
    under an UNGUESSABLE prefix `epoch/<epoch>-<reveal_nonce>/<hotkey>.json`. The
    nonce is a caller-supplied high-entropy string (never derived from clock/rng
    in-module, matching the no-wall-clock discipline). latest.json is written
    LAST and is the ONLY object that reveals the nonce, so the manifests are
    undiscoverable until the pointer flips. CNFs live at the stable `cnf/<sha>`
    path (deduped across epochs) but are just puzzle bodies — knowing a sha
    without the manifest is useless (you don't know which challenge/token it maps
    to, and the token carries not_before anyway).

    put_object(key, data, content_type) -> public_url.
    sign(bytes) -> base64 signature string.
    not_before: earliest submit time baked into every token (defaults to
    published_at when None).
    Returns a summary {epoch, miners, items, unique_cnfs, pointer_url, reveal_prefix}.
    """
    if not reveal_nonce or len(reveal_nonce) < 16:
        raise ValueError("reveal_nonce must be a high-entropy string (>=16 chars)")
    nbf = not_before or published_at
    reveal_prefix = f"epoch/{epoch}-{reveal_nonce}"
    uploaded_cnf: set[str] = set()
    total_items = 0
    manifest_keys: dict[str, str] = {}

    # Maps cnf sha -> the URL put_object returned for it. For a public-bucket
    # backend this is base+key; for a presigned-URL backend (Hippius) it is a
    # per-object signed GET url. Either way the manifest embeds the exact url a
    # miner GETs, so the read path is backend-agnostic.
    cnf_url_by_sha: dict[str, str] = {}

    for hk in hotkeys:
        items, cnfs = miner_assignment(
            hk, epoch, token_secret=token_secret, expires_at=expires_at,
            allotment_by_tier=allotment_by_tier, not_before=nbf)
        # Upload any CNFs not already uploaded this run (dedup by sha).
        for sha, body in cnfs.items():
            if sha not in uploaded_cnf:
                cnf_url_by_sha[sha] = put_object(
                    f"cnf/{sha}.cnf", body, "text/plain; charset=utf-8")
                uploaded_cnf.add(sha)
        # Attach the concrete read url to each item so miners never construct it.
        for it in items:
            it["cnf_url"] = cnf_url_by_sha.get(it["cnf_sha256"], "")
        manifest = {
            "schema": ASSIGNMENT_SCHEMA, "epoch": epoch, "miner_hotkey": hk,
            "published_at": published_at, "not_before": nbf, "expires_at": expires_at,
            "cnf_base": cnf_base_url, "count": len(items), "items": items,
        }
        manifest = sign_manifest(manifest, sign)
        body = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        key = f"{reveal_prefix}/{hk}.json"
        put_object(key, body, "application/json")
        manifest_keys[hk] = key
        total_items += len(items)
        if on_progress:
            on_progress("miner_published", {"hotkey": hk, "items": len(items)})

    # latest.json LAST — the only object that reveals the nonce, so manifests are
    # undiscoverable until this flips.
    pointer = {
        "schema": POINTER_SCHEMA, "epoch": epoch, "published_at": published_at,
        "not_before": nbf, "expires_at": expires_at,
        "reveal_prefix": reveal_prefix, "miner_count": len(manifest_keys),
    }
    pointer = sign_manifest(pointer, sign)
    pointer_url = put_object(
        "latest.json", json.dumps(pointer, separators=(",", ":")).encode("utf-8"),
        "application/json")

    return {
        "epoch": epoch, "miners": len(manifest_keys), "items": total_items,
        "unique_cnfs": len(uploaded_cnf), "pointer_url": pointer_url,
        "reveal_prefix": reveal_prefix,
    }
