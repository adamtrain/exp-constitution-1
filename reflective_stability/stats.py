"""Small, dependency-free statistics used by the preregistered analysis."""

import itertools
import math
import random
from collections import Counter
from statistics import mean

from .config import PERMUTATION_SEED, PERMUTATIONS


def sign_flip_test(values: list[float]) -> float:
    """Two-sided test that the mean of independent, symmetric-under-H0 values is zero.

    Exact when there are at most 16 values, otherwise Monte Carlo with a fixed seed.
    """
    if not values or all(v == 0 for v in values):
        return 1.0
    observed = abs(mean(values))
    if len(values) <= 16:
        signs = itertools.product((1, -1), repeat=len(values))
        flips = [abs(mean(s * v for s, v in zip(sign, values))) for sign in signs]
        return sum(f >= observed - 1e-12 for f in flips) / len(flips)
    rng = random.Random(PERMUTATION_SEED)
    hits = sum(
        abs(mean(v if rng.random() < 0.5 else -v for v in values)) >= observed - 1e-12 for _ in range(PERMUTATIONS)
    )
    return (hits + 1) / (PERMUTATIONS + 1)


def stratified_permutation_test(groups: dict[str, tuple[list[float], list[float]]]) -> tuple[float, float]:
    """Two-sided test of treatment minus control, shuffling labels only within each stratum (model).

    The statistic is the mean over strata of (treated mean - control mean), so every stratum
    counts equally even if some trajectories are missing. Returns (observed statistic, p-value).
    """

    def statistic(pairs):
        return mean(mean(treated) - mean(control) for treated, control in pairs)

    pairs = list(groups.values())
    observed = statistic(pairs)
    rng = random.Random(PERMUTATION_SEED)
    hits = 0
    for _ in range(PERMUTATIONS):
        shuffled = []
        for treated, control in pairs:
            pool = treated + control
            rng.shuffle(pool)
            shuffled.append((pool[: len(treated)], pool[len(treated) :]))
        hits += abs(statistic(shuffled)) >= abs(observed) - 1e-12
    return observed, (hits + 1) / (PERMUTATIONS + 1)


def holm(p_values: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted, running = {}, 0.0
    for rank, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - rank) * p))
        adjusted[name] = running
    return adjusted


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def krippendorff_alpha(units: list[list], metric: str = "nominal") -> float:
    """Krippendorff's alpha for units coded by several graders (missing codes already removed).

    metric: "nominal" (categories) or "interval" (numbers, squared difference).
    """
    units = [u for u in units if len(u) >= 2]
    if not units:
        return math.nan

    def distance(a, b):
        return (a - b) ** 2 if metric == "interval" else float(a != b)

    totals = Counter(v for unit in units for v in unit)
    n = sum(totals.values())
    observed = 0.0
    for unit in units:
        counts = Counter(unit)
        within = sum(counts[a] * counts[b] * distance(a, b) for a in counts for b in counts)
        observed += within / (len(unit) - 1)
    observed /= n
    expected = sum(totals[a] * totals[b] * distance(a, b) for a in totals for b in totals) / (n * (n - 1))
    return math.nan if expected == 0 else 1 - observed / expected
