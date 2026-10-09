from __future__ import annotations

from typing import Protocol

from .contracts import (
    ActionCandidate,
    ActionPrecondition,
    ExecutionResult,
    FailureCode,
    PredicateOperator,
    Scalar,
    StateSnapshot,
)


class ActionExecutor(Protocol):
    def execute(
        self,
        candidate: ActionCandidate | None,
        state: StateSnapshot,
        *,
        enforce_preconditions: bool,
    ) -> ExecutionResult: ...


def _compare(actual: Scalar, operator: PredicateOperator, expected: Scalar) -> bool:
    if operator is PredicateOperator.EQ:
        return actual == expected
    if operator is PredicateOperator.NE:
        return actual != expected
    if actual is None or expected is None:
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if operator is PredicateOperator.GT:
        return actual > expected  # type: ignore[operator]
    if operator is PredicateOperator.GE:
        return actual >= expected  # type: ignore[operator]
    if operator is PredicateOperator.LT:
        return actual < expected  # type: ignore[operator]
    if operator is PredicateOperator.LE:
        return actual <= expected  # type: ignore[operator]
    raise ValueError(f"unsupported predicate operator: {operator}")


def failed_preconditions(
    candidate: ActionCandidate,
    state: StateSnapshot,
) -> tuple[ActionPrecondition, ...]:
    return tuple(
        item
        for item in candidate.preconditions
        if not _compare(state.values.get(item.state_key), item.operator, item.expected)
    )


class DryRunExecutor:
    """Evaluate whether an action would be sent without mutating a device."""

    def execute(
        self,
        candidate: ActionCandidate | None,
        state: StateSnapshot,
        *,
        enforce_preconditions: bool,
    ) -> ExecutionResult:
        if candidate is None:
            return ExecutionResult(
                dry_run=True,
                applied=False,
                valid=None,
                checked_revision=state.revision,
                failure_code=FailureCode.NO_ACTION,
            )
        failed = failed_preconditions(candidate, state)
        valid = not failed
        if failed and enforce_preconditions:
            return ExecutionResult(
                dry_run=True,
                applied=False,
                valid=False,
                checked_revision=state.revision,
                failed_preconditions=failed,
                failure_code=FailureCode.PRECONDITION_FAILED,
            )
        return ExecutionResult(
            dry_run=True,
            applied=True,
            valid=valid,
            checked_revision=state.revision,
            failed_preconditions=failed,
            failure_code=(FailureCode.PRECONDITION_NOT_ENFORCED if failed else None),
        )
