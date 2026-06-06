"""
Deep triage for the 3 HydraDX Omnipool candidates.

C07: calculate_imbalance_in_hub_swap — impl >> proportional spec (but invariant holds)
C11: add_liquidity price drift from FixedU128 truncation
C17: sell_hub creates imbalance when L=0

Goal: classify each as real-new / intended / artifact, with independent witness re-derivation.
"""

from fractions import Fraction
import math

FIXED_U128_SCALE = 10**18

print("=" * 70)
print("TRIAGE: HydraDX Omnipool candidates")
print("=" * 70)

# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE C07 + C17: calculate_imbalance_in_hub_swap
# Code (math.rs:79-87):
#   num = delta_q * (Q - L)
#   denom = Q + delta_q
#   result = floor(num/denom) + 1 + delta_q
#
# Let's try to derive this from scratch:
# Imbalance invariant: f(Q, L) = Q*(Q-L)  must be non-increasing over time.
# When sell_hub happens: Q -> Q + dq, L -> L + delta_L (delta_L = delta_imbalance)
# Condition: Q*(Q-L) >= (Q+dq)*((Q+dq)-(L+delta_L))
#            Q²-QL >= (Q+dq)(Q+dq-L-delta_L)
#
# The code wants to find delta_L such that f is non-increasing.
# Minimum delta_L for invariant to hold:
#   (Q+dq)*(Q+dq-L-delta_L) <= Q*(Q-L)
#   (Q+dq)*(Q+dq-L) - (Q+dq)*delta_L <= Q²-QL
#   -(Q+dq)*delta_L <= Q²-QL - (Q+dq)²+(Q+dq)L
#   -(Q+dq)*delta_L <= Q²-QL - Q²-2Q·dq-dq²+QL+dq·L
#   -(Q+dq)*delta_L <= -2Q·dq-dq²+dq·L
#   -(Q+dq)*delta_L <= -dq*(2Q+dq-L)
#   (Q+dq)*delta_L >= dq*(2Q+dq-L)
#   delta_L >= dq*(2Q+dq-L) / (Q+dq)

print("\n--- C07/C17: Deriving minimum delta_L ---")
Q = 10_000_000_000_000_000
L = 1_000_000_000_000_000
dq = 100_000_000_000_000

min_delta_L = Fraction(dq * (2*Q + dq - L), Q + dq)
print(f"Q={Q}, L={L}, dq={dq}")
print(f"Minimum delta_L for invariant: {float(min_delta_L):.6e}")
print(f"min_delta_L exact (rational): {min_delta_L}")

# Code impl:
num = dq * (Q - L)
denom = Q + dq
impl_delta_L = (num // denom) + 1 + dq
print(f"Code impl delta_L: {impl_delta_L}")
print(f"Code impl = floor(dq*(Q-L)/(Q+dq)) + 1 + dq")

# Let's expand what the code gives vs the minimum needed:
# Code: floor(dq*(Q-L)/(Q+dq)) + 1 + dq
# Min:  dq*(2Q+dq-L)/(Q+dq)
#
# Simplify minimum:
# dq*(2Q+dq-L)/(Q+dq) = dq*(Q+dq + Q-L)/(Q+dq) = dq + dq*(Q-L)/(Q+dq)
#
# So minimum = dq + dq*(Q-L)/(Q+dq)
# Code =       dq + floor(dq*(Q-L)/(Q+dq)) + 1
#            = dq + floor(exact fractional part) + 1
#            >= dq + dq*(Q-L)/(Q+dq)   [since floor(x)+1 > x]
# QED: Code always satisfies minimum. The +1 is rounding up. The +dq matches exactly.

print(f"\nMinimum decomposed: dq + dq*(Q-L)/(Q+dq)")
frac_part = Fraction(dq * (Q - L), Q + dq)
print(f"  = {dq} + {float(frac_part):.6e}")
print(f"  = {float(dq + frac_part):.6e}")
print(f"Code ceiling of minimum: ceil({float(frac_part):.6e}) = {math.ceil(frac_part)}")
print(f"Code impl: {impl_delta_L}")
print(f"  = {dq} + {(num // denom)} + 1 = {dq} + floor + 1")
print(f"floor(frac_part) = {num // denom}, ceil = {math.ceil(frac_part)}")
print(f"Code = ceil(frac_part) + dq + 0 = {math.ceil(frac_part) + dq}")
print(f"Expected minimum = dq + ceil(frac_part) [since +1 = round up] = {dq + math.ceil(frac_part)}")

# Check: code == minimum (rounded up)?
print(f"\nCode == ceil(minimum)? {impl_delta_L == math.ceil(min_delta_L)}")
print(f"Code == floor(minimum)+1? {impl_delta_L == math.floor(min_delta_L) + 1}")
print(f"min_delta_L floor = {math.floor(min_delta_L)}")
print(f"min_delta_L ceil  = {math.ceil(min_delta_L)}")

print("\nKEY INSIGHT:")
print("The 'minimum' for the invariant to hold IS dq + dq*(Q-L)/(Q+dq)")
print("The code computes ceil(that minimum) = dq + floor(frac) + 1")
print("This is EXACTLY what's needed and the +dq term is NOT an error")
print("The code is correct — it's computing the exact minimum delta_L (rounded up)")

print("\n--- L=0 case (C17) ---")
Q2 = 10**15
L2 = 0
dq2 = 10**12
min_delta_L2 = Fraction(dq2 * (2*Q2 + dq2 - L2), Q2 + dq2)
impl2 = (dq2 * (Q2 - L2)) // (Q2 + dq2) + 1 + dq2
print(f"Q={Q2}, L={L2}, dq={dq2}")
print(f"Minimum delta_L for invariant: {float(min_delta_L2):.6e} = {math.ceil(min_delta_L2)}")
print(f"Code impl: {impl2}")
print(f"Code == ceil(minimum)? {impl2 == math.ceil(min_delta_L2)}")
print(f"When L=0: minimum = dq*(2Q+dq)/(Q+dq) ≈ 2dq (expected)")
print("CONCLUSION C17: When L=0, increasing imbalance by ~2dq IS CORRECT")
print("because the invariant f=Q*(Q-L) must stay non-increasing even for L=0 case.")
print("The formula is the exact mathematical minimum, not an over-burn.")

# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE C11: add_liquidity price drift from FixedU128 truncation
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("TRIAGE C11: add_liquidity price drift")

# Case: Q=3, R=7, amount=10^12
Q = 3
R = 7
amount = 10**12

# FixedU128::checked_from_rational(Q, R) = floor(Q/R * 10^18) = floor(3/7 * 10^18)
price_inner = (Q * FIXED_U128_SCALE) // R
print(f"\nQ={Q}, R={R}, amount={amount}")
print(f"price_inner = floor(3*10^18/7) = {price_inner}")
print(f"Exact price = {Fraction(3,7)} = {float(Fraction(3,7)):.20f}")
print(f"FixedU128 approx: {price_inner/FIXED_U128_SCALE:.20f}")
print(f"Error in price: {Fraction(3,7) - Fraction(price_inner, FIXED_U128_SCALE)}")

delta_hub_impl = (price_inner * amount) // FIXED_U128_SCALE
delta_hub_spec = Fraction(Q * amount, R)
print(f"delta_hub_impl = {delta_hub_impl}")
print(f"delta_hub_spec = {delta_hub_spec} = {float(delta_hub_spec):.6e}")
print(f"deficit = {math.ceil(delta_hub_spec) - delta_hub_impl}")

# After add:
R2 = R + amount
Q2 = Q + delta_hub_impl
Q2_spec = Q + math.ceil(delta_hub_spec)  # if rounded up

print(f"\nAfter add:")
print(f"R2 = {R2}, Q2_impl = {Q2}, Q2_spec(ceil) = {Q2_spec}")
print(f"Price impl: {Fraction(Q2, R2)} = {float(Fraction(Q2, R2)):.20f}")
print(f"Price spec: {Fraction(Q2_spec, R2)} = {float(Fraction(Q2_spec, R2)):.20f}")
print(f"Price orig: {Fraction(Q, R)} = {float(Fraction(Q, R)):.20f}")

# Can this be exploited? LP adds liquidity, price drifts down (hub/token ratio falls).
# Other LPs who added earlier see their hub value slightly diluted per share.
# But the drift is at most 1/amount which is 10^-12 here — negligible.
# Also the share calculation also rounds down, so the pool is net positive.

print(f"\nPrice drift magnitude: {float(abs(Fraction(Q2,R2) - Fraction(Q,R))):.2e}")
print(f"This is {float(abs(Fraction(Q2,R2) - Fraction(Q,R))) / float(Fraction(Q,R)) * 100:.2e}% drift")
print("CONCLUSION C11: Drift is <= 1/amount in hub terms, invariant preserved,")
print("no value extraction possible. INTENDED rounding (pool favour).")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print("C07 (imbalance formula >> proportional): ARTIFACT / INTENDED")
print("  The formula IS the exact minimum delta_L (rounded up) derived from the")
print("  Q*(Q-L) invariant. The +delta_q term is not an error; it's the dominant")
print("  term in the minimum required imbalance update. My 'spec' was wrong.")
print()
print("C11 (add_liquidity price drift 1 unit): INTENDED")
print("  FixedU128 truncation causes at most 1-unit deficit in delta_hub.")
print("  Price drift < 10^-12 relative. No value extraction. Normal rounding.")
print()
print("C17 (L=0 imbalance creation): ARTIFACT (same root as C07)")
print("  When L=0, the minimum required delta_L = dq*(2Q+dq)/(Q+dq) ≈ 2dq.")
print("  This is mathematically required to keep Q*(Q-L) non-increasing.")
print("  Not a bug. The comparison to 'proportional' was against the wrong spec.")
print()
print("HONEST VERDICT: 0 real-new findings.")
print("All 3 candidates triage to INTENDED/ARTIFACT after independent re-derivation.")
