"""REAL (non-planted) combinatorial challenge source — Phase A of the frontier
cube-and-conquer plan (deploy/V2_FRONTIER_CUBE_AND_CONQUER_PLAN_2026-07-01.md).

`per_miner.gen_planted_3sat` today produces instances *guaranteed* satisfiable
by a hidden planted assignment the publisher already knows — a proof-of-work
race over synthetic instances with a known answer baked in. This module is the
opt-in replacement: it mints genuine combinatorial-SAT instances (graph
k-coloring, Latin-square/Sudoku-style completion) whose CNF encoding does NOT
embed a returned solution. The miner must actually search for a satisfying
assignment; `dimacs.verify_witness` (unchanged, generic) is still the sole
correctness gate.

FLAG: CATHEDRAL_V2_CHALLENGE_SOURCE (default "planted" = current behaviour,
UNCHANGED). Values:
  planted        - unchanged: scaffold.dimacs.gen_planted_3sat via per_miner.
  combinatorial  - this module's graph-coloring / Latin-square generator.
  corpus         - load real .cnf files from CATHEDRAL_V2_CORPUS_DIR, selected
                   deterministically by (epoch, tier, seq). Falls back to the
                   combinatorial generator if the dir is unset/empty/missing.

Determinism: everything here is keyed by (epoch, tier, seq) ONLY — not the
miner hotkey. Real problems are a finite, expensive-to-mint resource; the
per-miner uniqueness / anti-copy property that the planted path gets from
hashing the hotkey into the seed is a planted-path-only guarantee. The
challenge_id miners see is still produced by per_miner.instance_id(), which
IS hotkey-HMAC'd, so token binding / the wire id format is unchanged — only
the CNF payload's source differs in the new modes.
"""
from __future__ import annotations

import hashlib
import os
import random


# --------------------------------------------------------------------------
# Flags / config
# --------------------------------------------------------------------------

def challenge_source() -> str:
    """CATHEDRAL_V2_CHALLENGE_SOURCE — default 'planted' (current behaviour)."""
    raw = os.environ.get("CATHEDRAL_V2_CHALLENGE_SOURCE", "").strip().lower()
    return raw if raw in ("combinatorial", "corpus") else "planted"


def corpus_dir() -> str | None:
    raw = os.environ.get("CATHEDRAL_V2_CORPUS_DIR", "").strip()
    return raw or None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# Small-but-real shapes: solve_cnf (the scaffold's tiny DPLL) must finish these
# in a few seconds, since it is not a competitive solver. Env-overridable.
_COLORING_SHAPE: dict[int, tuple[int, int]] = {1: (12, 3), 2: (16, 3)}
_LATIN_SHAPE: dict[int, int] = {1: 5, 2: 5}


def coloring_shape_for(tier: int) -> tuple[int, int]:
    base_n, base_k = _COLORING_SHAPE.get(tier, (8, 3))
    n = _env_int(f"CATHEDRAL_V2_COLORING_NODES_T{tier}", base_n)
    k = _env_int(f"CATHEDRAL_V2_COLORING_COLORS_T{tier}", base_k)
    return max(2, n), max(2, k)


def latin_shape_for(tier: int) -> int:
    base_n = _LATIN_SHAPE.get(tier, 4)
    return max(2, _env_int(f"CATHEDRAL_V2_LATIN_ORDER_T{tier}", base_n))


def _forced_kind() -> str | None:
    raw = os.environ.get("CATHEDRAL_V2_COMBINATORIAL_KIND", "").strip().lower()
    return raw if raw in ("coloring", "latin") else None


# --------------------------------------------------------------------------
# Deterministic seed / id derivation — keyed by (epoch, tier, seq) only
# --------------------------------------------------------------------------

def real_seed(epoch: int, tier: int, seq: int, salt: str = "") -> int:
    """Deterministic 63-bit seed. No secret mixed in: these instances carry no
    hidden witness worth protecting, and (per the module docstring) they are
    intentionally NOT per-hotkey."""
    raw = f"real:{salt}:{int(epoch)}:{int(tier)}:{int(seq)}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(h[:16], 16) & 0x7FFFFFFFFFFFFFFF


def kind_for(epoch: int, tier: int, seq: int, salt: str = "") -> str:
    forced = _forced_kind()
    if forced:
        return forced
    return "coloring" if real_seed(epoch, tier, seq, "kind:" + salt) % 2 == 0 else "latin"


def content_id_for(epoch: int, tier: int, seq: int, kind: str) -> str:
    """Opaque id for the CNF *content* (not the wire challenge_id — that is
    still per_miner.instance_id(), which is hotkey-HMAC'd). Useful for logs
    and for deterministic corpus-file selection."""
    return f"rc-{kind}-e{int(epoch)}-t{int(tier)}-s{int(seq)}"


# --------------------------------------------------------------------------
# Graph k-coloring
# --------------------------------------------------------------------------

def _find_coloring(n_nodes: int, k_colors: int, edges: list[tuple[int, int]]) -> list[int] | None:
    """Small deterministic backtracking search for a proper k-coloring. Used
    only to CONFIRM satisfiability at generation time (so we can regenerate a
    denser/sparser random graph if the draw happens to be uncolorable) — the
    result is never embedded in the returned CNF or handed back to callers."""
    adj: list[list[int]] = [[] for _ in range(n_nodes)]
    for u, v in edges:
        adj[u].append(v)
        adj[v].append(u)
    color = [-1] * n_nodes

    def bt(i: int) -> bool:
        if i == n_nodes:
            return True
        for c in range(k_colors):
            if all(color[nb] != c for nb in adj[i]):
                color[i] = c
                if bt(i + 1):
                    return True
                color[i] = -1
        return False

    return color if bt(0) else None


def gen_graph_coloring(seed: int, n_nodes: int, k_colors: int, *,
                       max_attempts: int = 500) -> str:
    """Deterministic, genuinely satisfiable k-coloring instance encoded as
    DIMACS CNF. The random graph draw is retried (deterministically, via a
    seeded RNG advancing across attempts) until it is k-colorable; the CNF
    encoding below never contains the discovered coloring — only the graph
    structure (vertex one-hot + edge inequality clauses), so an honest miner
    must search for a satisfying assignment same as the generator did.
    """
    rng = random.Random(seed)
    p = min(0.6, max(0.1, (k_colors - 1) / max(1, n_nodes) + 0.15))
    edges: list[tuple[int, int]] = []
    for _attempt in range(max(1, max_attempts)):
        candidate = [(i, j) for i in range(n_nodes) for j in range(i + 1, n_nodes)
                     if rng.random() < p]
        if _find_coloring(n_nodes, k_colors, candidate) is not None:
            edges = candidate
            break
    # else: exhausted attempts — fall back to the empty graph (trivially
    # k-colorable for k>=1), which keeps generation total/deterministic.

    def var(v: int, c: int) -> int:
        return v * k_colors + c + 1

    clauses: list[list[int]] = []
    for v in range(n_nodes):
        clauses.append([var(v, c) for c in range(k_colors)])       # >=1 color
        for c1 in range(k_colors):
            for c2 in range(c1 + 1, k_colors):
                clauses.append([-var(v, c1), -var(v, c2)])          # <=1 color
    for u, w in edges:
        for c in range(k_colors):
            clauses.append([-var(u, c), -var(w, c)])                # adjacent != same color

    n_vars = n_nodes * k_colors
    header = f"c graph-coloring n_nodes={n_nodes} k_colors={k_colors} edges={len(edges)}\n"
    header += f"p cnf {n_vars} {len(clauses)}\n"
    body = "".join(" ".join(map(str, cl)) + " 0\n" for cl in clauses)
    return header + body


# --------------------------------------------------------------------------
# Latin-square / Sudoku-style completion
# --------------------------------------------------------------------------

def _full_latin_square(seed: int, n: int) -> list[list[int]]:
    """A genuine random Latin square: a cyclic base square with independently
    shuffled row/column/symbol permutations applied."""
    rng = random.Random(seed)
    base = [[(r + c) % n for c in range(n)] for r in range(n)]
    row_perm = list(range(n)); rng.shuffle(row_perm)
    col_perm = list(range(n)); rng.shuffle(col_perm)
    sym_perm = list(range(n)); rng.shuffle(sym_perm)
    return [[sym_perm[base[row_perm[r]][col_perm[c]]] for c in range(n)] for r in range(n)]


def gen_latin_square(seed: int, n: int, *, mask_fraction: float = 0.45) -> str:
    """Deterministic Latin-square completion puzzle encoded as DIMACS CNF.

    A full valid Latin square is generated, then most cells are masked; the
    remaining cells become unit clauses ("clues"). The CNF does NOT reveal the
    masked cells' values anywhere outside those clue clauses — a solver must
    propagate the row/column/cell constraints to find A completion (not
    necessarily the original masked square; any valid completion satisfies
    verify_witness). This is the standard Sudoku-generator construction:
    genuine NP-hard completion search, no bias-exploitable shortcut.
    """
    grid = _full_latin_square(seed, n)
    rng = random.Random(seed ^ 0x5EED_5EED)
    cells = [(r, c) for r in range(n) for c in range(n)]
    rng.shuffle(cells)
    n_clues = max(1, int(round(len(cells) * (1.0 - max(0.0, min(1.0, mask_fraction))))))
    clues = {(r, c): grid[r][c] for (r, c) in cells[:n_clues]}

    def var(r: int, c: int, v: int) -> int:
        return (r * n + c) * n + v + 1

    clauses: list[list[int]] = []
    for r in range(n):
        for c in range(n):
            clauses.append([var(r, c, v) for v in range(n)])        # cell has >=1 value
            for v1 in range(n):
                for v2 in range(v1 + 1, n):
                    clauses.append([-var(r, c, v1), -var(r, c, v2)])  # cell has <=1 value
    for r in range(n):
        for v in range(n):
            clauses.append([var(r, c, v) for c in range(n)])        # value appears in row
    for c in range(n):
        for v in range(n):
            clauses.append([var(r, c, v) for r in range(n)])        # value appears in column
    for (r, c), v in clues.items():
        clauses.append([var(r, c, v)])                               # given clue

    n_vars = n * n * n
    header = f"c latin-square n={n} clues={len(clues)}\n"
    header += f"p cnf {n_vars} {len(clauses)}\n"
    body = "".join(" ".join(map(str, cl)) + " 0\n" for cl in clauses)
    return header + body


# --------------------------------------------------------------------------
# Public generator: combinatorial (in-process, no external files)
# --------------------------------------------------------------------------

def generate_combinatorial_instance(epoch: int, tier: int, seq: int, salt: str = "") -> tuple[str, str]:
    """Deterministic in (epoch, tier, seq, salt). Returns (content_id, dimacs_text).
    No planted/known solution is returned or embedded — genuinely unplanted.
    `salt` (the miner hotkey on the live per-miner path) makes the instance
    per-miner so two miners with the same (epoch,tier,seq) get different puzzles
    and cannot copy each other's answer. Empty salt = the legacy shared form."""
    kind = kind_for(epoch, tier, seq, salt)
    if kind == "coloring":
        n_nodes, k_colors = coloring_shape_for(tier)
        seed = real_seed(epoch, tier, seq, "coloring:" + salt)
        cnf_text = gen_graph_coloring(seed, n_nodes, k_colors)
    else:
        n = latin_shape_for(tier)
        seed = real_seed(epoch, tier, seq, "latin:" + salt)
        cnf_text = gen_latin_square(seed, n)
    return content_id_for(epoch, tier, seq, kind), cnf_text


# --------------------------------------------------------------------------
# Public generator: on-disk corpus (SAT Competition / SATLIB style .cnf files)
# --------------------------------------------------------------------------

def _corpus_files(directory: str) -> list[str]:
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.lower().endswith((".cnf", ".dimacs")) and
            os.path.isfile(os.path.join(directory, f))
        )
    except OSError:
        return []
    return names


def load_corpus_instance(epoch: int, tier: int, seq: int, salt: str = "") -> tuple[str, str] | None:
    """Deterministically select and load a real .cnf file from
    CATHEDRAL_V2_CORPUS_DIR. Returns None if the dir is unset/empty/missing —
    callers should fall back to generate_combinatorial_instance().
    """
    directory = corpus_dir()
    if not directory or not os.path.isdir(directory):
        return None
    files = _corpus_files(directory)
    if not files:
        return None
    idx = real_seed(epoch, tier, seq, "corpus:" + salt) % len(files)
    fname = files[idx]
    path = os.path.join(directory, fname)
    with open(path, "r", encoding="utf-8", errors="strict") as fh:
        text = fh.read()
    return f"corpus-{fname}-e{int(epoch)}-t{int(tier)}-s{int(seq)}", text


def generate_real_instance(epoch: int, tier: int, seq: int, salt: str = "") -> tuple[str, str]:
    """Top-level dispatcher used by per_miner.generate_instance for the
    'combinatorial' and 'corpus' sources. Returns (content_id, dimacs_text).
    `salt` (miner hotkey on the live path) makes the instance per-miner."""
    if challenge_source() == "corpus":
        loaded = load_corpus_instance(epoch, tier, seq, salt)
        if loaded is not None:
            return loaded
        # Empty/unset CATHEDRAL_V2_CORPUS_DIR: fall back rather than error.
    return generate_combinatorial_instance(epoch, tier, seq, salt)
