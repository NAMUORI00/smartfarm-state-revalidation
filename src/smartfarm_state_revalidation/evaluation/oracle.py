"""Deterministic oracles for structured state claims and action preconditions.

The module intentionally accepts plain mappings.  It does not import the runtime
contracts, an LLM, or a human-labelled score so that the same frozen records can
be evaluated after product contract changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Iterable, Literal, Mapping, Sequence


_MISSING = object()
OracleStatus = Literal["satisfied", "violated", "not_applicable", "protocol_error"]


@dataclass(frozen=True)
class PredicateResult:
    status: OracleStatus
    satisfied: bool | None
    operator: str
    state_key: str | None = None
    observed: Any = None
    expected: Any = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OracleResult:
    status: OracleStatus
    satisfied: bool | None
    numerator: int
    denominator: int
    predicates: tuple[PredicateResult, ...]
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "satisfied": self.satisfied,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "error_count": self.error_count,
            "predicates": [item.to_dict() for item in self.predicates],
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def state_value(state: Mapping[str, Any], state_key: str) -> Any:
    """Resolve an exact flat key first, then a dot-separated nested path."""

    if state_key in state:
        return state[state_key]
    current: Any = state
    for part in state_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _leaf_result(spec: Mapping[str, Any], state: Mapping[str, Any]) -> PredicateResult:
    state_key = spec.get("state_key", spec.get("key"))
    operator = str(spec.get("operator", spec.get("relation", spec.get("comparison", "eq")))).lower()
    if not isinstance(state_key, str) or not state_key:
        return PredicateResult(
            status="protocol_error",
            satisfied=None,
            operator=operator,
            reason="missing_state_key",
        )

    observed = state_value(state, state_key)
    expected = spec.get("expected", spec.get("value"))
    if operator in {"exists", "present"}:
        satisfied = observed is not _MISSING and observed is not None
        return PredicateResult(
            status="satisfied" if satisfied else "violated",
            satisfied=satisfied,
            operator=operator,
            state_key=state_key,
            observed=None if observed is _MISSING else observed,
            reason=None if satisfied else "missing_state",
        )
    if operator in {"missing", "absent"}:
        satisfied = observed is _MISSING or observed is None
        return PredicateResult(
            status="satisfied" if satisfied else "violated",
            satisfied=satisfied,
            operator=operator,
            state_key=state_key,
            observed=None if observed is _MISSING else observed,
        )
    if observed is _MISSING:
        return PredicateResult(
            status="violated",
            satisfied=False,
            operator=operator,
            state_key=state_key,
            expected=expected,
            reason="missing_state",
        )

    try:
        if operator in {"eq", "equals", "exact"}:
            satisfied = observed == expected
        elif operator in {"ne", "not_equals"}:
            satisfied = observed != expected
        elif operator == "lt":
            satisfied = observed < expected
        elif operator in {"lte", "le"}:
            satisfied = observed <= expected
        elif operator == "gt":
            satisfied = observed > expected
        elif operator in {"gte", "ge"}:
            satisfied = observed >= expected
        elif operator == "in":
            if isinstance(expected, (str, bytes)) or not isinstance(expected, Iterable):
                raise TypeError("in requires a non-string iterable")
            satisfied = observed in expected
        elif operator == "not_in":
            if isinstance(expected, (str, bytes)) or not isinstance(expected, Iterable):
                raise TypeError("not_in requires a non-string iterable")
            satisfied = observed not in expected
        elif operator in {"between", "range"}:
            bounds = _mapping(expected)
            lower = bounds.get("lower")
            upper = bounds.get("upper")
            if lower is None or upper is None:
                raise TypeError("between requires lower and upper")
            lower_ok = observed >= lower if bounds.get("lower_inclusive", True) else observed > lower
            upper_ok = observed <= upper if bounds.get("upper_inclusive", True) else observed < upper
            satisfied = lower_ok and upper_ok
        elif operator in {"approx_eq", "within"}:
            tolerance = float(spec.get("tolerance", 0.0))
            satisfied = abs(float(observed) - float(expected)) <= tolerance
        else:
            return PredicateResult(
                status="protocol_error",
                satisfied=None,
                operator=operator,
                state_key=state_key,
                observed=observed,
                expected=expected,
                reason="unsupported_operator",
            )
    except (TypeError, ValueError) as error:
        return PredicateResult(
            status="protocol_error",
            satisfied=None,
            operator=operator,
            state_key=state_key,
            observed=observed,
            expected=expected,
            reason=f"comparison_error:{type(error).__name__}",
        )
    return PredicateResult(
        status="satisfied" if satisfied else "violated",
        satisfied=satisfied,
        operator=operator,
        state_key=state_key,
        observed=observed,
        expected=expected,
    )


def _expression_result(spec: Mapping[str, Any], state: Mapping[str, Any]) -> PredicateResult:
    for logical_operator in ("all", "any"):
        if logical_operator not in spec:
            continue
        children = spec[logical_operator]
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)) or not children:
            return PredicateResult(
                status="protocol_error",
                satisfied=None,
                operator=logical_operator,
                reason="logical_operator_requires_nonempty_list",
            )
        evaluated = [
            _expression_result(child, state)
            if isinstance(child, Mapping)
            else PredicateResult("protocol_error", None, logical_operator, reason="predicate_must_be_mapping")
            for child in children
        ]
        if any(item.status == "protocol_error" for item in evaluated):
            return PredicateResult("protocol_error", None, logical_operator, reason="child_protocol_error")
        satisfied = (
            all(item.satisfied is True for item in evaluated)
            if logical_operator == "all"
            else any(item.satisfied is True for item in evaluated)
        )
        return PredicateResult("satisfied" if satisfied else "violated", satisfied, logical_operator)
    if "not" in spec:
        child = spec["not"]
        if not isinstance(child, Mapping):
            return PredicateResult("protocol_error", None, "not", reason="predicate_must_be_mapping")
        evaluated = _expression_result(child, state)
        if evaluated.status == "protocol_error":
            return PredicateResult("protocol_error", None, "not", reason="child_protocol_error")
        satisfied = not bool(evaluated.satisfied)
        return PredicateResult("satisfied" if satisfied else "violated", satisfied, "not")
    return _leaf_result(spec, state)


def _aggregate(predicates: Sequence[Mapping[str, Any]], state: Mapping[str, Any]) -> OracleResult:
    if not predicates:
        return OracleResult("not_applicable", None, 0, 0, ())
    results = tuple(_expression_result(item, state) for item in predicates)
    error_count = sum(item.status == "protocol_error" for item in results)
    denominator = sum(item.status in {"satisfied", "violated"} for item in results)
    numerator = sum(item.status == "satisfied" for item in results)
    if error_count:
        return OracleResult("protocol_error", None, numerator, denominator, results, error_count)
    satisfied = numerator == denominator
    return OracleResult(
        "satisfied" if satisfied else "violated",
        satisfied,
        numerator,
        denominator,
        results,
    )


def evaluate_response_claims(
    claims: Sequence[Mapping[str, Any]],
    response_state: Mapping[str, Any],
    *,
    required_keys: Sequence[str] = (),
    expected_revision: int | None = None,
    expected_units: Mapping[str, str] | None = None,
    state_rules: Mapping[str, Mapping[str, Any]] | None = None,
) -> OracleResult:
    """Check every machine-readable response claim against response-time state."""

    if not required_keys:
        return _aggregate(claims, response_state)

    by_key: dict[str, list[Mapping[str, Any]]] = {}
    expected_units = expected_units or {}
    state_rules = state_rules or {}
    for claim in claims:
        key = claim.get("key", claim.get("state_key"))
        if isinstance(key, str):
            by_key.setdefault(key, []).append(claim)

    results: list[PredicateResult] = []
    for key in required_keys:
        matching = by_key.get(key, [])
        if len(matching) != 1:
            results.append(
                PredicateResult(
                    status="violated",
                    satisfied=False,
                    operator="eq",
                    state_key=key,
                    reason="missing_required_claim" if not matching else "duplicate_required_claim",
                )
            )
            continue
        claim = matching[0]
        result = _leaf_result(claim, response_state)
        if result.satisfied is False and key in state_rules:
            comparison = compare_state_value(
                claim.get("value", claim.get("expected")),
                state_value(response_state, key),
                state_rules[key],
            )
            if not comparison["changed"] and not comparison.get("protocol_error"):
                result = PredicateResult(
                    status="satisfied",
                    satisfied=True,
                    operator="eq",
                    state_key=key,
                    observed=state_value(response_state, key),
                    expected=claim.get("value", claim.get("expected")),
                )
        observed_revision = claim.get("observed_revision")
        expected_unit = expected_units.get(key)
        if (
            result.satisfied is True
            and expected_revision is not None
            and observed_revision != expected_revision
        ):
            result = PredicateResult(
                status="violated",
                satisfied=False,
                operator="eq",
                state_key=key,
                observed=result.observed,
                expected=result.expected,
                reason="stale_observed_revision",
            )
        elif result.satisfied is True and expected_unit is not None and claim.get("unit") != expected_unit:
            result = PredicateResult(
                status="violated",
                satisfied=False,
                operator="eq",
                state_key=key,
                observed=result.observed,
                expected=result.expected,
                reason="unit_mismatch",
            )
        results.append(result)

    denominator = len(results)
    numerator = sum(item.satisfied is True for item in results)
    error_count = sum(item.status == "protocol_error" for item in results)
    if error_count:
        status: OracleStatus = "protocol_error"
        satisfied: bool | None = None
    else:
        satisfied = numerator == denominator
        status = "satisfied" if satisfied else "violated"
    return OracleResult(status, satisfied, numerator, denominator, tuple(results), error_count)


def evaluate_preconditions(
    preconditions: Sequence[Mapping[str, Any]],
    execution_state: Mapping[str, Any],
) -> OracleResult:
    """Check whether all declared action preconditions hold at execution time."""

    return _aggregate(preconditions, execution_state)


def _semantic_band(value: float, bands: Sequence[Mapping[str, Any]]) -> str | None:
    for index, band in enumerate(bands):
        lower_ok = True
        upper_ok = True
        if "lower_inclusive" in band:
            lower_ok = value >= float(band["lower_inclusive"])
        if "lower_exclusive" in band:
            lower_ok = lower_ok and value > float(band["lower_exclusive"])
        if "upper_inclusive" in band:
            upper_ok = value <= float(band["upper_inclusive"])
        if "upper_exclusive" in band:
            upper_ok = upper_ok and value < float(band["upper_exclusive"])
        if lower_ok and upper_ok:
            return str(band.get("label", f"band_{index}"))
    return None


def compare_state_value(before: Any, after: Any, rule: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compare one state value using a predeclared deterministic rule."""

    rule = rule or {}
    if before is _MISSING or after is _MISSING:
        return {
            "changed": True,
            "before": None if before is _MISSING else before,
            "after": None if after is _MISSING else after,
            "reason": "missing_required_key",
        }
    kind = str(rule.get("kind", "categorical"))
    numeric = kind.startswith("numeric_") or any(
        key in rule for key in ("absolute_epsilon", "minimum_meaningful_delta", "semantic_bands")
    )
    if not numeric:
        return {
            "changed": before != after,
            "before": before,
            "after": after,
            "reason": "exact_mismatch" if before != after else "exact_match",
        }
    try:
        before_number = float(before)
        after_number = float(after)
    except (TypeError, ValueError):
        return {
            "changed": True,
            "before": before,
            "after": after,
            "reason": "non_numeric_value",
            "protocol_error": True,
        }
    if not isfinite(before_number) or not isfinite(after_number):
        return {
            "changed": True,
            "before": before,
            "after": after,
            "reason": "non_finite_value",
            "protocol_error": True,
        }
    delta = abs(after_number - before_number)
    if "minimum_meaningful_delta" in rule:
        threshold = float(rule["minimum_meaningful_delta"])
    else:
        threshold = float(rule.get("absolute_epsilon", 0.0))
    before_band = _semantic_band(before_number, rule.get("semantic_bands", []))
    after_band = _semantic_band(after_number, rule.get("semantic_bands", []))
    band_changed = before_band != after_band
    changed = delta > threshold or band_changed
    return {
        "changed": changed,
        "before": before,
        "after": after,
        "absolute_delta": delta,
        "threshold": threshold,
        "before_band": before_band,
        "after_band": after_band,
        "reason": "numeric_change" if changed else "within_tolerance",
    }


def protocol_state_rules(protocol: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rules: dict[str, Mapping[str, Any]] = {}
    for item in protocol.get("state_keys", []):
        if isinstance(item, Mapping) and isinstance(item.get("key"), str):
            rules[str(item["key"])] = item
    return rules


def related_state_keys(record: Mapping[str, Any], protocol: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = record.get("related_state_keys")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        return tuple(sorted({str(item) for item in explicit}))
    action = _mapping(record.get("action"))
    profile_id = record.get("dependency_profile_id", action.get("dependency_profile_id"))
    for profile in protocol.get("dependency_profiles", []):
        if isinstance(profile, Mapping) and profile.get("profile_id") == profile_id:
            keys = profile.get("required_state_keys", [])
            if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes)):
                return tuple(sorted({str(item) for item in keys}))
    return ()


def detect_related_changes(
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
    keys: Sequence[str],
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rules = protocol_state_rules(protocol or {})
    comparisons = {
        key: compare_state_value(
            state_value(before_state, key),
            state_value(after_state, key),
            rules.get(key),
        )
        for key in sorted(set(keys))
    }
    changed_keys = [key for key, result in comparisons.items() if result["changed"]]
    return {
        "changed": bool(changed_keys),
        "changed_keys": changed_keys,
        "comparisons": comparisons,
        "protocol_error_count": sum(bool(result.get("protocol_error")) for result in comparisons.values()),
    }


def _sequence(value: Any) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


_CANDIDATE_FIELDS = ("action", "target", "value", "bound_state_revision")


def _candidate_matches(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        return actual is None and expected is None
    return all(actual.get(field) == expected.get(field) for field in _CANDIDATE_FIELDS)


def evaluate_action_candidate(
    actual: Any,
    expected: Any,
    truth_preconditions: Sequence[Mapping[str, Any]],
    execution_truth: Mapping[str, Any],
) -> OracleResult:
    if not isinstance(actual, Mapping):
        return OracleResult("not_applicable", None, 0, 0, ())
    if not isinstance(expected, Mapping):
        predicate = PredicateResult(
            status="violated",
            satisfied=False,
            operator="candidate_identity",
            reason="unexpected_candidate",
        )
        return OracleResult("violated", False, 0, 1, (predicate,))

    identity_results = tuple(
        PredicateResult(
            status="satisfied" if actual.get(field) == expected.get(field) else "violated",
            satisfied=actual.get(field) == expected.get(field),
            operator="eq",
            state_key=f"candidate.{field}",
            observed=actual.get(field),
            expected=expected.get(field),
            reason=None if actual.get(field) == expected.get(field) else "candidate_mismatch",
        )
        for field in _CANDIDATE_FIELDS
    )
    precondition_result = evaluate_preconditions(truth_preconditions, execution_truth)
    results = identity_results + precondition_result.predicates
    error_count = precondition_result.error_count
    denominator = len(identity_results) + precondition_result.denominator
    numerator = sum(item.satisfied is True for item in identity_results) + precondition_result.numerator
    if error_count:
        return OracleResult("protocol_error", None, numerator, denominator, results, error_count)
    satisfied = numerator == denominator
    return OracleResult(
        "satisfied" if satisfied else "violated",
        satisfied,
        numerator,
        denominator,
        results,
    )


def evaluate_episode(record: Mapping[str, Any], protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one executed record into judge-free episode outcomes."""

    protocol = protocol or {}
    states = _mapping(record.get("states"))
    actual_initial_state = _mapping(states.get("initial", record.get("initial_state")))
    actual_response_state = _mapping(states.get("response", record.get("response_state")))
    actual_execution_state = _mapping(states.get("execution", record.get("execution_state")))
    oracle = _mapping(record.get("oracle"))
    initial_state = _mapping(oracle.get("initial_truth")) or actual_initial_state
    response_state = _mapping(oracle.get("release_truth")) or actual_response_state
    execution_state = _mapping(oracle.get("execution_truth")) or actual_execution_state
    if not response_state:
        response_state = execution_state
    response = _mapping(record.get("response"))
    action = _mapping(record.get("action"))
    outcome = _mapping(record.get("outcome"))
    claims = _sequence(record.get("response_claims", response.get("claims", ())))
    truth_preconditions = _sequence(oracle.get("execution_preconditions"))

    dependency_keys = related_state_keys(record, protocol)
    revisions = _mapping(record.get("state_revisions"))
    response_revision = revisions.get("response", record.get("response_state_revision"))
    enforce_response_revision = record.get("enforce_response_revision") is True
    state_rules = protocol_state_rules(protocol)
    expected_units = {
        key: str(state_rules[key]["unit"])
        for key in dependency_keys
        if key in state_rules and isinstance(state_rules[key].get("unit"), str)
    }
    claim_result = evaluate_response_claims(
        claims,
        response_state,
        required_keys=dependency_keys,
        expected_revision=(
            response_revision
            if enforce_response_revision and isinstance(response_revision, int)
            else None
        ),
        expected_units=expected_units,
        state_rules=state_rules,
    )
    actual_candidate = action.get("candidate")
    expected_candidate = oracle.get("expected_action_candidate")
    action_result = evaluate_action_candidate(
        actual_candidate,
        expected_candidate,
        truth_preconditions,
        execution_state,
    )
    changes = detect_related_changes(initial_state, response_state or execution_state, dependency_keys, protocol)
    actual_release_mismatch = detect_related_changes(
        actual_response_state,
        response_state,
        dependency_keys,
        protocol,
    )
    actual_execution_mismatch = detect_related_changes(
        actual_execution_state,
        execution_state,
        dependency_keys,
        protocol,
    )

    refresh_required = changes["changed"] if dependency_keys else None
    refresh_performed = outcome.get("refresh_performed", outcome.get("refreshed"))
    if not isinstance(refresh_performed, bool):
        refresh_performed = None
    executed = outcome.get("executed", record.get("executed"))
    if not isinstance(executed, bool):
        executed = None
    initial_candidate = oracle.get("initial_candidate")
    truth_precondition_result = evaluate_preconditions(truth_preconditions, execution_state)
    invalid_action_opportunity = isinstance(initial_candidate, Mapping) and (
        not _candidate_matches(initial_candidate, expected_candidate)
        or truth_precondition_result.satisfied is False
    )
    valid_action_opportunity = (
        isinstance(expected_candidate, Mapping) and truth_precondition_result.satisfied is True
    )
    candidate_retained = isinstance(actual_candidate, Mapping)
    opportunity_error_count = 0
    declared_invalid = oracle.get("declared_invalid_action_opportunity")
    declared_valid = oracle.get("declared_valid_action_opportunity")
    if isinstance(declared_invalid, bool) and declared_invalid != invalid_action_opportunity:
        opportunity_error_count += 1
    if isinstance(declared_valid, bool) and declared_valid != valid_action_opportunity:
        opportunity_error_count += 1
    execution_error_count = int(invalid_action_opportunity and executed is None)

    change_expected = changes["changed"] if dependency_keys else None
    change_detected = outcome.get("change_detected")
    if not isinstance(change_detected, bool):
        detected_keys = outcome.get("detected_state_keys")
        if isinstance(detected_keys, Sequence) and not isinstance(detected_keys, (str, bytes)):
            expected_keys = set(changes["changed_keys"])
            change_detected = expected_keys.issubset({str(item) for item in detected_keys}) if expected_keys else None
        else:
            change_detected = None

    return {
        "episode_id": str(record.get("episode_id", "")),
        "pair_id": str(record.get("pair_id", record.get("episode_id", ""))),
        "condition_id": str(record.get("condition_id", "")),
        "comparison_scope": str(record.get("comparison_scope", "paired_primary")),
        "scenario_category": record.get("scenario_category"),
        "failure_code": record.get("failure_code"),
        "repetition": int(record.get("repetition", 0)),
        "response_claim_consistent": claim_result.satisfied,
        "response_claim_consistent_count": claim_result.numerator,
        "response_claim_count": claim_result.denominator,
        "response_claim_oracle": claim_result.to_dict(),
        "action_valid": action_result.satisfied,
        "action_precondition_oracle": action_result.to_dict(),
        "executed": executed,
        "invalid_action_opportunity": invalid_action_opportunity,
        "valid_action_opportunity": valid_action_opportunity,
        "candidate_retained": candidate_retained,
        "refresh_required": refresh_required,
        "refresh_performed": refresh_performed,
        "change_expected": change_expected,
        "change_detected": change_detected,
        "related_change": changes,
        "oracle_state_match": not actual_release_mismatch["changed"] and not actual_execution_mismatch["changed"],
        "calls": dict(_mapping(record.get("calls"))),
        # 답변 단위 회계는 실행 기록에서 그대로 옮긴다. 오라클 판정 대상이 아니라
        # 선택적 수정의 생성비용과 수정 범위를 집계하기 위한 값이다.
        "answer_units": dict(_mapping(record.get("answer_units"))),
        "latencies_ms": dict(_mapping(record.get("latencies_ms"))),
        "protocol_error_count": (
            claim_result.error_count
            + action_result.error_count
            + int(changes["protocol_error_count"])
            + int(actual_release_mismatch["changed"])
            + int(actual_execution_mismatch["changed"])
            + opportunity_error_count
            + execution_error_count
        ),
    }
