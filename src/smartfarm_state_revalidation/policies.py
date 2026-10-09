from __future__ import annotations

from dataclasses import dataclass

from .contracts import Condition


@dataclass(frozen=True, slots=True)
class RevalidationPolicy:
    condition: Condition
    max_refreshes: int
    block_action_on_release_change: bool
    enforce_execution_preconditions: bool
    selective_revision: bool = False
    """참이면 갱신 시 전체가 아니라 영향을 받은 답변 단위만 다시 만든다."""


B0_ONE_SHOT = RevalidationPolicy(
    condition=Condition.B0,
    max_refreshes=0,
    block_action_on_release_change=False,
    enforce_execution_preconditions=False,
)

B1_BLOCK_ONLY = RevalidationPolicy(
    condition=Condition.B1,
    max_refreshes=0,
    block_action_on_release_change=True,
    enforce_execution_preconditions=True,
)

B2_MAX_ONE_REFRESH = RevalidationPolicy(
    condition=Condition.B2,
    max_refreshes=1,
    block_action_on_release_change=True,
    enforce_execution_preconditions=True,
)


SR_SELECTIVE_REVISION = RevalidationPolicy(
    condition=Condition.SR,
    max_refreshes=1,
    block_action_on_release_change=True,
    enforce_execution_preconditions=True,
    selective_revision=True,
)


_POLICIES = {
    Condition.B0: B0_ONE_SHOT,
    Condition.B1: B1_BLOCK_ONLY,
    Condition.B2: B2_MAX_ONE_REFRESH,
    Condition.SR: SR_SELECTIVE_REVISION,
}


def policy_for(condition: Condition | str) -> RevalidationPolicy:
    return _POLICIES[Condition(condition)]

