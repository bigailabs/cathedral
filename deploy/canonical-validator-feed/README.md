# Canonical SN39 validator feed origin

This origin exposes only Cathedral's signed validator vector, its JWKS, and
liveness. The publisher stays loopback-only and validators continue to enforce
signature, freshness, subnet, policy, and rollback checks.

## Invariant

All supported public reads must return the same signed vector bytes:

- `https://api.cathedral.computer/v1/validator/weights/next`
- `https://api.cathedral.computer/api/cathedral/v1/validator/weights/next`
- `https://read.cathedral.computer/v1/validator/weights/next`

Both public hostnames serve
`/.well-known/cathedral-jwks.json`. The JWKS entry whose `kid` equals the
vector's `key_id` must verify the exact vector bytes.

## Installation contract

1. Confirm the scorer is healthy on loopback and emits the required signed
   policy.
2. Obtain a certificate containing both public hostnames using a DNS challenge.
3. Install `nginx.conf`, run `nginx -t`, and reload Nginx.
4. Allow TCP 443 only from the reverse proxy's published address ranges.
5. Change the existing DNS records in place, retaining their IDs and previous
   values as rollback anchors.
6. Run `scripts/validator_release_gate.py` and the latest validator in
   `--offline --once` mode before enabling any chain broadcast.

## Rollback

Restore the prior DNS record values, remove the Nginx site symlink, validate and
reload Nginx, and remove the scoped firewall rule. The validator must remain on
its known-good burn vector throughout rollback.
