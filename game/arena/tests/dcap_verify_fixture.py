"""A protocol-faithful stand-in for the real DCAP verifier command
(attestor-verify / polaris_verify). The arena's CommandIntelVerifier invokes it
as `<cmd> quote.bin report_data_hex result.json`; it must exit 0 and write JSON
with intel_verified. This proves the REAL command-verifier plumbing runs; in
production the same hook points at the Go DCAP binary that checks Intel's chain.
"""
import json
import sys


def main() -> int:
    quote_path, report_data_hex, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    quote = open(quote_path, "rb").read()
    # a real verifier checks the PCK chain; this fixture asserts the structural
    # shape the real binary also requires (>=632 bytes) and echoes the verdict.
    ok = len(quote) >= 632 and len(report_data_hex) == 128
    json.dump({"intel_verified": ok, "ok": ok, "report_data_match": ok},
              open(result_path, "w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
