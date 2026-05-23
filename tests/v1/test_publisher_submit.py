"""POST /v1/agents/submit — per CONTRACTS.md §2.1, §4.1, §6, §7.1.

Each test cites the contract section it pins. If the implementation
diverges, the failure message points the implementer at the section.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from tests.v1.conftest import (
    CONTRACT_HOTKEY_HEADER,
    CONTRACT_SIGNATURE_HEADER,
    _now_iso_ms,
    blake3_hex,
    make_bundle_without_soul,
    make_invalid_zip,
    make_oversized_bundle,
    make_valid_bundle,
    sign_submission_payload,
    submit_multipart,
)

# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_submit_happy_path_returns_202_with_id_and_bundle_hash(
    publisher_client, alice_keypair
):
    """CONTRACTS.md §2.1 — response 202 contains id, bundle_hash, status, submitted_at."""
    bundle = make_valid_bundle()
    expected_hash = blake3_hex(bundle)

    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        display_name="Alice's EU AI Act Analyst",
    )

    assert resp.status_code == 202, (
        f"§2.1 happy path must return 202 Accepted, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "id" in body, "§2.1 response missing `id`"
    assert body.get("bundle_hash") == expected_hash, (
        f"§2.1+§4.4 bundle_hash must equal blake3 of uploaded bytes, "
        f"got {body.get('bundle_hash')!r} expected {expected_hash!r}"
    )
    assert body.get("status") == "pending_check", (
        f"§6 step 1: initial status must be 'pending_check', got {body.get('status')!r}"
    )
    assert "submitted_at" in body, "§2.1 response missing `submitted_at`"
    # ISO-8601 UTC trailing Z (Section 9 lock #6)
    assert body["submitted_at"].endswith("Z"), (
        f"§9 lock #6: timestamps must end with 'Z', got {body['submitted_at']!r}"
    )


def test_submit_insert_uses_shared_db_write_lock(
    publisher_client,
    alice_keypair,
    monkeypatch,
):
    """SAT finalization and public submits share one SQLite commit gate."""
    from cathedral.publisher import repository

    original_insert = repository.insert_agent_submission
    lock_states: list[bool] = []

    async def wrapped_insert(*args, **kwargs):
        lock_states.append(publisher_client.app.state.ctx.db_write_lock.locked())
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(repository, "insert_agent_submission", wrapped_insert)

    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=make_valid_bundle(soul_md="# shared write lock probe\n"),
        display_name="Shared Lock Probe",
    )

    assert resp.status_code == 202
    assert lock_states == [True]


def test_submit_id_is_lowercase_hyphenated_uuid(publisher_client, alice_keypair):
    """CONTRACTS.md §9 lock #7: UUIDs are lowercase hyphenated 8-4-4-4-12."""
    import uuid

    bundle = make_valid_bundle(soul_md="# unique soul text\n")
    resp = submit_multipart(
        publisher_client, keypair=alice_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert resp.status_code == 202
    body = resp.json()
    parsed = uuid.UUID(body["id"])
    # str(UUID) is lowercase hyphenated by default.
    assert str(parsed) == body["id"], (
        f"§9 lock #7: id must be lowercase hyphenated, got {body['id']!r}"
    )


# --------------------------------------------------------------------------
# Bad signature → 401
# --------------------------------------------------------------------------


def test_submit_rejects_bad_signature_with_401(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — `401 invalid hotkey signature`."""
    bundle = make_valid_bundle(soul_md="# bad sig probe\n")
    # Random base64 of correct length (sr25519 sig is 64 bytes).
    bogus_sig = base64.b64encode(secrets.token_bytes(64)).decode("ascii")

    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        override_signature=bogus_sig,
    )
    assert resp.status_code == 401, (
        f"§2.1: bad sig must yield 401, got {resp.status_code}: {resp.text}"
    )
    assert "detail" in resp.json(), "§9 lock #3: error envelope must be {detail: str}"


def test_submit_rejects_missing_signature_header_with_401(
    publisher_client, alice_keypair
):
    """CONTRACTS.md §2.1+§4.1 — missing X-Cathedral-Signature is unauthenticated."""
    bundle = make_valid_bundle(soul_md="# missing sig\n")
    submitted_at = _now_iso_ms()
    files = {"bundle": ("agent.zip", bundle, "application/zip")}
    data = {
        "card_id": "eu-ai-act",
        "display_name": "x",
        "submitted_at": submitted_at,
    }
    headers = {CONTRACT_HOTKEY_HEADER: alice_keypair.ss58_address}
    resp = publisher_client.post(
        "/v1/agents/submit", headers=headers, data=data, files=files
    )
    assert resp.status_code == 401, (
        f"§2.1: missing signature header must be 401, got {resp.status_code}"
    )


def test_submit_rejects_hotkey_mismatch_with_401(
    publisher_client, alice_keypair, bob_keypair
):
    """CONTRACTS.md §2.1 — `bad/missing signature, hotkey doesn't match payload`.

    Sign with Alice's key but claim Bob's hotkey in the header. The
    signature won't verify against Bob's ss58.
    """
    bundle = make_valid_bundle(soul_md="# hotkey mismatch\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        override_hotkey=bob_keypair.ss58_address,
    )
    assert resp.status_code == 401, (
        f"§2.1: hotkey/sig mismatch must be 401, got {resp.status_code}"
    )


def test_submit_rejects_lying_about_bundle_hash_with_401(
    publisher_client, alice_keypair
):
    """CONTRACTS.md §2.1 paragraph after error list — cathedral computes
    the hash itself and refuses signatures that claim a different value."""
    bundle = make_valid_bundle(soul_md="# liar liar\n")
    real_hash = blake3_hex(bundle)
    fake_hash = blake3_hex(bundle + b"tamper")
    submitted_at = _now_iso_ms()

    # Sign the FAKE hash — the upload will compute the real one.
    sig = sign_submission_payload(
        alice_keypair,
        bundle_hash=fake_hash,
        card_id="eu-ai-act",
        submitted_at=submitted_at,
    )
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        submitted_at=submitted_at,
        override_signature=sig,
        override_bundle_hash=fake_hash,  # also tells our helper to re-use
    )
    assert resp.status_code == 401, (
        f"§2.1: signature over wrong bundle_hash must be 401, "
        f"got {resp.status_code}; real={real_hash[:8]}.. fake={fake_hash[:8]}.. "
        f"body={resp.text}"
    )


# --------------------------------------------------------------------------
# Duplicate submissions
# --------------------------------------------------------------------------


def test_submit_duplicate_bundle_hash_same_hotkey_returns_409(
    publisher_client, alice_keypair
):
    """CONTRACTS.md §2.1 + §3.2 (idx_agent_unique) — duplicate (hotkey,card,hash) → 409."""
    bundle = make_valid_bundle(soul_md="# duplicate me\n")

    first = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        display_name="First",
    )
    assert first.status_code == 202, f"first submission should succeed: {first.text}"

    # Re-submit with the SAME bundle, same hotkey, same card.
    second = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        display_name="Second attempt",
    )
    assert second.status_code == 409, (
        f"§2.1: duplicate bundle_hash must yield 409, got {second.status_code}: "
        f"{second.text}"
    )


def test_submit_duplicate_bundle_hash_cross_hotkey_returns_409(
    publisher_client, alice_keypair, bob_keypair
):
    """CONTRACTS.md §7.1 check 1 + §7.3 — exact bundle duplicate across hotkeys
    is rejected as `exact bundle duplicate`."""
    bundle = make_valid_bundle(soul_md="# cross-hotkey duplicate\n")

    first = submit_multipart(
        publisher_client, keypair=alice_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert first.status_code == 202

    second = submit_multipart(
        publisher_client, keypair=bob_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert second.status_code == 409, (
        f"§7.1.1+§7.3: cross-hotkey same-bundle on same card must be 409 "
        f"(rejection_reason='exact bundle duplicate'), got {second.status_code}: "
        f"{second.text}"
    )


# --------------------------------------------------------------------------
# Malformed multipart / missing fields
# --------------------------------------------------------------------------


def test_submit_missing_bundle_returns_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — missing required `bundle` field is 422."""
    submitted_at = _now_iso_ms()
    sig = sign_submission_payload(
        alice_keypair,
        bundle_hash="0" * 64,
        card_id="eu-ai-act",
        submitted_at=submitted_at,
    )
    resp = publisher_client.post(
        "/v1/agents/submit",
        headers={
            CONTRACT_SIGNATURE_HEADER: sig,
            CONTRACT_HOTKEY_HEADER: alice_keypair.ss58_address,
        },
        data={
            "card_id": "eu-ai-act",
            "display_name": "no bundle",
            "submitted_at": submitted_at,
        },
    )
    assert resp.status_code in {400, 422}, (
        f"§2.1: missing bundle file should be 400 or 422 (FastAPI default), "
        f"got {resp.status_code}: {resp.text}"
    )


def test_submit_unsupported_card_id_returns_404(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — unknown card_id is 404 'card not found'."""
    bundle = make_valid_bundle(soul_md="# unknown card probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="this-card-does-not-exist",
        bundle=bundle,
    )
    # Contract says 404; the older 422 prose is in the prompt but the
    # error list in §2.1 is explicit: "404 — card_id not found".
    assert resp.status_code == 404, (
        f"§2.1 error list: unknown card_id is 404, got {resp.status_code}: {resp.text}"
    )
    assert "detail" in resp.json()


def test_submit_oversized_bundle_returns_413_or_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — `400 — bundle exceeds 10 MiB limit`. The task
    brief acknowledges 413 or 422 are also acceptable depending on stack.
    """
    bundle = make_oversized_bundle()
    assert len(bundle) > 10 * 1024 * 1024, "test fixture must exceed 10 MiB"
    resp = submit_multipart(
        publisher_client, keypair=alice_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert resp.status_code in {400, 413, 422}, (
        f"§2.1: bundle >10MiB must be rejected with 400/413/422, "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


def test_submit_invalid_zip_returns_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — `422 — bundle structure invalid (missing soul.md, not a zip)`."""
    bundle = make_invalid_zip()
    resp = submit_multipart(
        publisher_client, keypair=alice_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert resp.status_code == 422, (
        f"§2.1: invalid zip must be 422, got {resp.status_code}: {resp.text[:200]}"
    )


def test_submit_zip_without_soul_md_returns_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — `422 — bundle missing required file: soul.md`."""
    bundle = make_bundle_without_soul()
    resp = submit_multipart(
        publisher_client, keypair=alice_keypair, card_id="eu-ai-act", bundle=bundle
    )
    assert resp.status_code == 422, (
        f"§2.1: zip without soul.md must be 422, got {resp.status_code}: {resp.text[:200]}"
    )


def test_submit_display_name_too_long_returns_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — display_name 1-64 chars."""
    bundle = make_valid_bundle(soul_md="# long display name probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        display_name="x" * 200,
    )
    assert resp.status_code in {400, 422}, (
        f"§2.1: display_name >64 chars must be 400 or 422, got {resp.status_code}"
    )


def test_submit_bio_too_long_returns_422(publisher_client, alice_keypair):
    """CONTRACTS.md §2.1 — bio max 280 chars."""
    bundle = make_valid_bundle(soul_md="# long bio probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        bio="x" * 1000,
    )
    assert resp.status_code in {400, 422}, (
        f"§2.1: bio >280 chars must be 400 or 422, got {resp.status_code}"
    )


# --------------------------------------------------------------------------
# Similarity collision (Section 7.1)
# --------------------------------------------------------------------------


def test_submit_metadata_fingerprint_collision_rejected(
    publisher_client, alice_keypair, bob_keypair
):
    """CONTRACTS.md §7.1 check 4 — same metadata_fingerprint (display_name +
    bundle_size_bucket) on same card from a different hotkey → reject with
    rejection_reason='metadata fingerprint duplicate'.

    We construct two different bundles with the same payload size (after
    rounding to 1 KiB buckets) and the same NFKC-normalized display name,
    then submit from two different hotkeys.
    """
    # Pad both bundles to the same 1KiB-bucketed size with identical filler.
    target_pad = b"X" * 4096  # forces both into the same 1 KiB bucket
    bundle_a = make_valid_bundle(
        soul_md="# Soul A — bundle one\n", extra_files={"pad.bin": target_pad}
    )
    bundle_b = make_valid_bundle(
        soul_md="# Soul B — different inside\n",
        extra_files={"pad.bin": target_pad, "extra.txt": b"differs"},
    )
    # Recompute B with extra padding to land in the same KiB bucket as A.
    bucket_a = len(bundle_a) // 1024
    bucket_b = len(bundle_b) // 1024
    if bucket_b != bucket_a:
        # Adjust padding to align buckets (cheap, deterministic).
        delta_bytes = max(0, (bucket_a - bucket_b) * 1024)
        bundle_b = make_valid_bundle(
            soul_md="# Soul B — different inside\n",
            extra_files={
                "pad.bin": target_pad,
                "filler.bin": b"y" * (4096 + delta_bytes),
            },
        )

    # Display name normalizes to the same NFKC-lower-collapsed string.
    display = "Compliance  Sentinel"
    first = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle_a,
        display_name=display,
    )
    assert first.status_code == 202, f"first submission must succeed: {first.text}"

    second = submit_multipart(
        publisher_client,
        keypair=bob_keypair,
        card_id="eu-ai-act",
        bundle=bundle_b,
        display_name="compliance sentinel",  # normalizes equal
    )
    # The contract is explicit that this is a rejection; it can either be
    # surfaced as an HTTP error (4xx) or accepted with status='rejected' +
    # rejection_reason. Both paths satisfy §7.1 — the row exists with the
    # rejection_reason set.
    if second.status_code == 202:
        body = second.json()
        # Allow async detection: implementer may persist the row then
        # mark rejected after the synchronous similarity check completes.
        # Either way, status must end up 'rejected' before the response,
        # because §6 step 2 says similarity check is SYNCHRONOUS inside
        # the submit handler.
        assert body.get("status") == "rejected", (
            f"§6 step 2 + §7.1: similarity check is synchronous, must yield "
            f"status='rejected' in the 202 response; got status={body.get('status')!r}"
        )
        assert body.get("rejection_reason"), (
            f"§7.1.4: rejection must populate rejection_reason; body={body}"
        )
    else:
        assert second.status_code in {409, 422}, (
            f"§7.1.4: metadata fingerprint duplicate must be rejected (4xx) or "
            f"persisted with status=rejected; got {second.status_code}: {second.text}"
        )


def test_submit_display_name_fuzzy_collision_within_7_days_rejected(
    publisher_client, alice_keypair, bob_keypair
):
    """CONTRACTS.md §7.1 check 3 — Levenshtein ratio >= 0.85 + within 7d → reject."""
    bundle_a = make_valid_bundle(soul_md="# Sentinel A\n")
    first = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle_a,
        display_name="Compliance Sentinel",
    )
    assert first.status_code == 202

    # Different bundle (hash differs), one-letter display change.
    bundle_b = make_valid_bundle(soul_md="# Sentinel B - different\n")
    second = submit_multipart(
        publisher_client,
        keypair=bob_keypair,
        card_id="eu-ai-act",
        bundle=bundle_b,
        display_name="Complience Sentinel",  # one-letter typo, ratio ~0.95
    )
    if second.status_code == 202:
        body = second.json()
        assert body.get("status") == "rejected", (
            f"§7.1.3: display_name Levenshtein >= 0.85 within 7 days must yield "
            f"status='rejected'; got {body.get('status')!r}"
        )
        assert body.get("rejection_reason"), "§7.1.3: rejection_reason required"
    else:
        assert second.status_code in {409, 422}, (
            f"§7.1.3: fuzzy display name collision must be rejected; "
            f"got {second.status_code}: {second.text}"
        )


# --------------------------------------------------------------------------
# Error envelope shape (Section 9 lock #3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "bad_card",
        "bad_zip",
    ],
)
def test_error_envelope_shape(publisher_client, alice_keypair, case):
    """CONTRACTS.md §9 lock #3 — every error is `{"detail": "<string>"}`."""
    if case == "bad_card":
        bundle = make_valid_bundle(soul_md=f"# {case}\n")
        resp = submit_multipart(
            publisher_client,
            keypair=alice_keypair,
            card_id="nope-not-real",
            bundle=bundle,
        )
    else:
        resp = submit_multipart(
            publisher_client,
            keypair=alice_keypair,
            card_id="eu-ai-act",
            bundle=make_invalid_zip(),
        )

    assert resp.status_code >= 400, f"setup error in case={case!r}"
    body = resp.json()
    assert isinstance(body, dict), f"§9 lock #3: error body must be dict, got {type(body)}"
    assert "detail" in body, f"§9 lock #3: error body must have `detail` key; got {body}"
    assert isinstance(body["detail"], str), (
        f"§9 lock #3: detail must be string, got {type(body['detail'])}"
    )
    # No extra keys (envelope is exactly {detail: str}).
    extra = set(body.keys()) - {"detail"}
    assert not extra, f"§9 lock #3: error envelope must be exactly {{detail}}; got extras {extra}"
