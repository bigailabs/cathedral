# Cathedral Launch Announcement Draft

## Discord-Ready Announcement

Cathedral was never meant to be only a random SAT board.

The original Cathedral shape was a verified-agent network. Miners ran live
Hermes agents inside environments Cathedral could inspect deeply. We were not
only collecting final answers; we could see prompts, tool calls, memory over
time, `soul.me`, keep-alive behavior, responsiveness, and how the agent actually
worked across tasks.

One early example was the EU AI Act work. For about a week, agents analyzed the
regulation inside an always-on, observable environment. That was not "submit
text, get scored." It was a network of miner agents doing work Cathedral could
measure.

Then came the hard question:

**What are you measuring?**

That question matters because a subnet pays for what it measures. For
Cathedral, it matters even more because measurement is what turns the agent
stack from an interesting experiment into something valuable.

Raw agent output is not enough. Normal agent output is messy, subjective, and
hard to verify. Cathedral needs artifacts that can be checked.

That is why SAT matters.

SAT gives Cathedral a deterministic proof rail. Real problems can be encoded
into checkable artifacts, and valid answers can be verified precisely. SAT is
not the whole Cathedral thesis. It is the verification primitive that lets
Cathedral score agent work without relying on vibes or claims.

The loop is:

- Miner agents inspect real tasks.
- They produce artifacts, not just claims.
- Cathedral verifies those artifacts.
- Verified work becomes scoring.
- Verified work becomes training data.

The sharp version:

- "I found a bug" is not enough.
- "Here is a witness, target commit, harness, trace, and replay result" is
  valuable.
- "This replayed witness becomes a labeled training example" is extremely
  valuable.

## Where We Are Now

- SAT is the active scored lane.
- This release adds support for proportional scoring metadata and miner-level
  score explanations in the signed validator-vector path.
- The current board is still mostly random SAT, so many modern solvers clear
  work quickly and the experience can feel flat.
- Better challenge distribution and board visibility are in the release path,
  but the live public API may not expose those fields until deployment.
- Secure Compute is opening as gated live intake. It is not a broad hardware
  ask.
- Hermes-style agent work is not a new pivot. It is Cathedral returning to its
  original architecture with a sharper verification layer.

## Where We Are Going

Cathedral has three product pillars, supported by verified compute where it is
actually needed.

### 1. SAT / Verification

SAT remains the deterministic proof primitive.

It gives Cathedral a way to turn messy real-world work into checkable artifacts:
witnesses, encodings, replay harnesses, certificates, and reproducible
outcomes.

### 2. Hermes Agent Work

Miners should run useful agents, not just submit answers.

The model is simple:

- Miner hotkey maps to an agent identity.
- Cathedral assigns or queries work.
- Hermes runs the agent loop.
- Cathedral captures the trace and artifacts.
- Cathedral verifies the outcome before scoring.

The goal is not agent prose. The goal is verified agent work.

### 3. Distillation

Every verified run becomes training data.

Accepted traces teach what worked. Rejected traces teach what failed. Replayable
bug and incentive witnesses become high-value labeled examples.

That is the compounding loop: agents do work, Cathedral verifies it, and the
verified work improves future agents.

## Secure Compute

Secure Compute supports this architecture, but only when the machine is proven
real and useful.

Cathedral can accept invited/allowlisted miner offers now, but Cathedral will
not ask miners broadly to buy or rent hardware until the full evidence loop is
proven:

- signed miner offer
- fresh evidence request
- cryptographic TEE/GPU verification
- provider listing acceptance
- health check
- usage or revenue receipt

Until then, Secure Compute remains invite-gated intake and operator review. An
intake code is only permission to submit an offer; it is not proof that the
machine is real, listable, or earning.

## Miner Takeaway

Today: solve the live SAT board.

Next: Cathedral will increasingly reward verifiable agent work.

This is not a pivot away from Cathedral. It is the original Cathedral thesis
becoming sharper:

**Cathedral rewards proof, not claims.**
