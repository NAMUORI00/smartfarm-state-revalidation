"""Small, dependency-free statistical helpers for the frozen evaluation."""

from __future__ import annotations

from math import comb, sqrt
from random import Random
from statistics import NormalDist, fmean
from typing import Any, Iterable, Sequence


def wilson_interval(
    successes: int,
    trials: int,
    *,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Return a Wilson score interval without converting a zero denominator to zero."""

    if successes < 0 or trials < 0 or successes > trials:
        raise ValueError("successes and trials must satisfy 0 <= successes <= trials")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if trials == 0:
        return {
            "status": "not_applicable",
            "method": "wilson_score",
            "confidence": confidence,
            "successes": successes,
            "trials": trials,
            "lower": None,
            "upper": None,
        }
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    half_width = (
        z
        * sqrt((proportion * (1 - proportion) + z * z / (4 * trials)) / trials)
        / denominator
    )
    return {
        "status": "ok",
        "method": "wilson_score",
        "confidence": confidence,
        "successes": successes,
        "trials": trials,
        "lower": max(0.0, center - half_width),
        "upper": min(1.0, center + half_width),
    }


def zero_event_upper_bound(trials: int, *, confidence: float = 0.95) -> dict[str, Any]:
    """Exact one-sided binomial upper bound when no events are observed."""

    if trials < 0:
        raise ValueError("trials must be non-negative")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    return {
        "status": "ok" if trials else "not_applicable",
        "method": "exact_binomial_zero_event_upper",
        "confidence": confidence,
        "trials": trials,
        "upper": 1 - (1 - confidence) ** (1 / trials) if trials else None,
    }


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    position = (len(sorted_values) - 1) * probability
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index
    return sorted_values[lower_index] + (sorted_values[upper_index] - sorted_values[lower_index]) * fraction


def paired_difference_bootstrap(
    baseline: Iterable[float | int | bool | None],
    treatment: Iterable[float | int | bool | None],
    *,
    confidence: float = 0.95,
    resamples: int = 10_000,
    seed: int = 20_260_807,
) -> dict[str, Any]:
    """Deterministic percentile bootstrap of a paired treatment-minus-baseline mean."""

    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    raw_pairs = zip(baseline, treatment, strict=True)
    pairs = [(float(left), float(right)) for left, right in raw_pairs if left is not None and right is not None]
    if not pairs:
        return {
            "status": "not_applicable",
            "method": "paired_percentile_bootstrap",
            "confidence": confidence,
            "paired_count": 0,
            "baseline_mean": None,
            "treatment_mean": None,
            "difference": None,
            "lower": None,
            "upper": None,
            "resamples": resamples,
            "seed": seed,
        }
    differences = [right - left for left, right in pairs]
    random = Random(seed)
    count = len(differences)
    bootstrapped = sorted(
        fmean(differences[random.randrange(count)] for _ in range(count))
        for _ in range(resamples)
    )
    alpha = 1 - confidence
    return {
        "status": "ok",
        "method": "paired_percentile_bootstrap",
        "confidence": confidence,
        "paired_count": count,
        "baseline_mean": fmean(left for left, _ in pairs),
        "treatment_mean": fmean(right for _, right in pairs),
        "difference": fmean(differences),
        "lower": _quantile(bootstrapped, alpha / 2),
        "upper": _quantile(bootstrapped, 1 - alpha / 2),
        "resamples": resamples,
        "seed": seed,
    }


def paired_binary_exact(
    baseline: Iterable[bool | int | float | None],
    treatment: Iterable[bool | int | float | None],
) -> dict[str, Any]:
    """Return paired binary counts and an exact two-sided McNemar p-value."""

    observed_pairs = [
        (left, right)
        for left, right in zip(baseline, treatment, strict=True)
        if left is not None and right is not None
    ]
    if any(left not in {0, 1, False, True} or right not in {0, 1, False, True} for left, right in observed_pairs):
        return {
            "status": "not_applicable",
            "method": "exact_mcnemar_binomial",
            "reason": "non_binary_pairs",
            "paired_count": len(observed_pairs),
            "improved": None,
            "worsened": None,
            "unchanged": None,
            "difference": None,
            "p_value_two_sided": None,
        }
    pairs = [(bool(left), bool(right)) for left, right in observed_pairs]
    if not pairs:
        return {
            "status": "not_applicable",
            "method": "exact_mcnemar_binomial",
            "paired_count": 0,
            "improved": 0,
            "worsened": 0,
            "unchanged": 0,
            "difference": None,
            "p_value_two_sided": None,
        }
    improved = sum(not left and right for left, right in pairs)
    worsened = sum(left and not right for left, right in pairs)
    discordant = improved + worsened
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(comb(discordant, index) for index in range(0, min(improved, worsened) + 1))
        p_value = min(1.0, 2 * tail / (2**discordant))
    return {
        "status": "ok",
        "method": "exact_mcnemar_binomial",
        "paired_count": len(pairs),
        "improved": improved,
        "worsened": worsened,
        "unchanged": len(pairs) - discordant,
        "difference": fmean(float(right) - float(left) for left, right in pairs),
        "p_value_two_sided": p_value,
    }
