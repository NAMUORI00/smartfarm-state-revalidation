from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any

from .contracts import FieldChange, RelatedStateSpec, Scalar, StateDiff, StateSnapshot


_MISSING = object()


def _canonical_scalar(value: Scalar | object) -> Any:
    if value is _MISSING:
        return {"missing": True}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("state values must not contain NaN or infinity")
        if value == 0:
            return 0.0
    return value


def related_state_payload(snapshot: StateSnapshot, spec: RelatedStateSpec) -> dict[str, Any]:
    return {
        dependency.key: {
            "value": _canonical_scalar(snapshot.values.get(dependency.key, _MISSING)),
            "unit": dependency.unit,
        }
        for dependency in spec.dependencies
    }


def fingerprint(snapshot: StateSnapshot, spec: RelatedStateSpec) -> str:
    payload = json.dumps(
        related_state_payload(snapshot, spec),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _crossed_boundary(before: float, after: float, boundaries: tuple[float, ...]) -> bool:
    return any((before < boundary <= after) or (after < boundary <= before) for boundary in boundaries)


def _change_reason(
    before: Scalar | object,
    after: Scalar | object,
    *,
    tolerance: float,
    boundaries: tuple[float, ...],
) -> str | None:
    if before is _MISSING or after is _MISSING:
        return None if before is after else "presence_changed"
    if isinstance(before, bool) or isinstance(after, bool):
        return None if before == after else "value_changed"
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        before_number = float(before)
        after_number = float(after)
        if not math.isfinite(before_number) or not math.isfinite(after_number):
            raise ValueError("state values must not contain NaN or infinity")
        if _crossed_boundary(before_number, after_number, boundaries):
            return "boundary_crossed"
        return "tolerance_exceeded" if abs(after_number - before_number) > tolerance else None
    return None if before == after else "value_changed"


def compare_related(
    before: StateSnapshot,
    after: StateSnapshot,
    spec: RelatedStateSpec,
) -> StateDiff:
    changes: list[FieldChange] = []
    for dependency in spec.dependencies:
        before_value = before.values.get(dependency.key, _MISSING)
        after_value = after.values.get(dependency.key, _MISSING)
        reason = _change_reason(
            before_value,
            after_value,
            tolerance=dependency.tolerance,
            boundaries=dependency.boundaries,
        )
        if reason is not None:
            changes.append(
                FieldChange(
                    key=dependency.key,
                    before=None if before_value is _MISSING else before_value,
                    after=None if after_value is _MISSING else after_value,
                    reason=reason,
                )
            )
    return StateDiff(
        before_hash=fingerprint(before, spec),
        after_hash=fingerprint(after, spec),
        changes=tuple(changes),
    )
