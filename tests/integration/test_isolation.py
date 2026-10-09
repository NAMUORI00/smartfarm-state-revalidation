from __future__ import annotations

import pytest

from smartfarm_state_revalidation.isolation import IsolationError, assert_research_environment


def _safe_environment() -> dict[str, str]:
    return {
        "THESIS_SIMULATION_ONLY": "true",
        "THESIS_COMPOSE_PROJECT_NAME": "smartfarm-thesis-state-revalidation-test",
        "THESIS_TELEMETRY_BASE_URL": "http://telemetry:8001",
        "THESIS_FARM_MCP_BASE_URL": "http://farm-mcp:8002",
    }


def test_accepts_isolated_internal_endpoints() -> None:
    assert_research_environment(_safe_environment())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("THESIS_SIMULATION_ONLY", "false"),
        ("THESIS_COMPOSE_PROJECT_NAME", "smartfarm-evidence-workspace"),
        ("THESIS_TELEMETRY_BASE_URL", "http://127.0.0.1:8020"),
        ("THESIS_FARM_MCP_BASE_URL", "http://127.0.0.1:8010"),
    ],
)
def test_rejects_product_environment(key: str, value: str) -> None:
    environment = _safe_environment()
    environment[key] = value
    with pytest.raises(IsolationError):
        assert_research_environment(environment)
