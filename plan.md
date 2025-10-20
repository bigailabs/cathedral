# Plan: Integrate Pricing Into Basilica CLI

## Goals
- Surface hourly pricing directly in `basilica ls` (available nodes) and `basilica ps` (active rentals).
- Remove the redundant `basilica price` and `basilica usage` subcommands while preserving balance functionality.
- Ensure displayed prices match what billing now charges when a rental is active.

## Implementation Steps
1. **Collect Pricing Data**
   - Extend `handle_ls` and `handle_ps` to fetch billing packages via `BasilicaClient::get_packages`.
   - Normalize package metadata into an in-memory map keyed by GPU model/category for price lookups.
   - In `handle_ps`, also call `BasilicaClient::list_usage_history` to obtain hourly rate + accumulated cost for active rentals (limit sized to cover current rentals).

2. **Command Pipeline Changes**
   - Remove the `Price` and `Usage` variants from `Commands` in `cli/commands.rs`, update `requires_auth`, and prune associated match arms in `cli/args.rs`.
   - Delete `handlers::price` and `handlers::usage` modules (and their exports from `handlers/mod.rs`), moving any reusable helpers into `gpu_rental.rs` as needed.
   - Update CLI help/quick-start strings so they no longer reference the removed subcommands.

3. **`basilica ls` Output Updates**
   - Pass the pricing map into `table_output::display_available_nodes_{compact,detailed}`.
   - Add hourly price columns:
     - Detailed/default view: show per-node hourly cost (including GPU count multiplier when appropriate).
     - Compact view: include the representative hourly rate for the grouped GPU type.
   - Handle missing price data gracefully (e.g., display `-` or "Unknown") and keep JSON mode behavior unchanged unless we decide to enrich it.

4. **`basilica ps` Output Updates**
   - Build a `HashMap<rental_id, RentalUsageRecord>` from the usage response.
   - Extend `table_output::display_rental_items` to accept optional pricing info and render columns for `Rate/hr` and `Total Cost`, covering compact/standard/detailed table variants.
   - Fall back to `-` if billing data is unavailable for a rental, and keep the footer totals consistent (e.g., sum of total cost if data present).

5. **Formatting + Helpers**
   - Centralize currency formatting (parse decimal strings, apply GPU count multiplier) inside `table_output` or a new helper to avoid duplication.
   - Ensure price calculations mirror billing logic (rate per GPU × GPU count); flag if this assumption differs from expected behavior.

6. **Cleanup & Docs**
   - Remove now-unused pricing/usage table helpers and tests.
   - Update `CHANGELOG.md` (and any other docs) to describe the new UX and the removal of standalone commands.
   - Search the repo for references to `basilica price` / `basilica usage` and update or delete related content.

7. **Verification**
   - Run targeted CLI unit/integration tests (if present) or add new ones for the updated table helpers.
   - Manually sanity-check formatting functions with representative sample data (multiple GPUs, missing packages).

## Open Questions / Follow-ups
- Confirm expected hourly rate for multi-GPU nodes: should CLI display the per-node total (`rate × gpu_count`) or the per-GPU rate? Current assumption is to show the total amount a renter would pay per hour for that node.
- Determine whether JSON output for `ls`/`ps` should embed pricing; currently planning to leave JSON mirroring API responses unless requirements say otherwise.

