# Scoring

Implements issue #3. The card scorer is in `cathedral.cards.score`.

## Six dimensions

Each in `[0.0, 1.0]`:

| Dimension | What it measures | Default weight |
|---|---|---|
| `source_quality` | Share of citations from official classes; required-class coverage bonus | 0.30 |
| `maintenance` | Time since last refresh vs cadence | 0.20 |
| `freshness` | Continuous decay from cadence to 4× cadence | 0.15 |
| `specificity` | Length of `what_changed` + `why_it_matters` | 0.15 |
| `usefulness` | Action notes + risks + confidence | 0.10 |
| `clarity` | Summary length and sentence count | 0.10 |

Sum of weights = 1.0. Tunable via `cathedral.types.ScoreParts.weighted` (subclass to override).

## Official source classes

`government`, `regulator`, `court`, `parliament`, `law_text`, `official_journal`.

A card whose citations are 100% official scores `1.0` on the base; non-official citations dilute it linearly. Required-class coverage adds up to `+0.20` when the card matches the registry entry's required classes.

## Freshness curve

```
ratio = age_hours / cadence_hours
ratio <= 1.0  ->  1.0
ratio >= 4.0  ->  0.0
otherwise     ->  1.0 - (ratio - 1.0) / 3.0
```

## Maintenance bands

| Age | Score |
|---|---|
| `<= cadence` | 1.0 |
| `<= 2× cadence` | 0.6 |
| `<= 4× cadence` | 0.2 |
| `> 4× cadence` | 0.0 |

## Preflight (failure modes)

Cards fail before scoring if:

- No citations
- Any citation HTTP status outside `200..400`
- Missing `no_legal_advice` marker
- Empty `summary`, `what_changed`, or `why_it_matters`
- Legal-advice framing (`you should`, `we recommend`, `as your lawyer`, etc.)

A failed card receives no score; the claim is rejected with `preflight: <reason>`.

## Baseline registry

`CardRegistry.baseline()` seeds five cards: `eu-ai-act`, `us-ai-eo`, `uk-ai-whitepaper`, `singapore-pdpc`, `japan-meti-mic`. Each uses the same six-dimension scoring rubric and the same preflight contract; only the `required_source_classes` tuple and `refresh_cadence_hours` differ per jurisdiction. Operators can override via TOML in a future config field; for now, edit `cathedral.cards.registry` directly.

## Runtime multiplier

v1 uses a `1.00x` runtime multiplier for every scored submission. The only live emissions path is BYO Box (`ssh-probe`): Cathedral SSHs into the miner-declared host, runs Hermes during the eval window, captures the trace bundle, and scores the produced card.

| Path | Runner | Multiplier | Live in v1 |
|---|---|---|---|
| BYO Box (`ssh-probe`) | `SshProbeRunner` / `SshHermesRunner` | 1.00x | yes |
| Discovery (`unverified`) | none | no score | yes |
| self-TEE (`tee`) | hardware verifier | 1.00x | spec-only; no live TEE miners |
