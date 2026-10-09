from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse


PRODUCT_COMPOSE_PROJECT = "smartfarm-evidence-workspace"
RESEARCH_COMPOSE_PREFIX = "smartfarm-thesis-state-revalidation-"
PRODUCT_HOST_PORTS = frozenset({3100, 8000, 8010, 8020, 6333, 6379})


class IsolationError(RuntimeError):
    pass


def assert_research_environment(environment: Mapping[str, str]) -> None:
    if environment.get("THESIS_SIMULATION_ONLY", "").lower() != "true":
        raise IsolationError("THESIS_SIMULATION_ONLY=true is required")

    project = environment.get("THESIS_COMPOSE_PROJECT_NAME", "")
    if project == PRODUCT_COMPOSE_PROJECT or not project.startswith(RESEARCH_COMPOSE_PREFIX):
        raise IsolationError("a thesis-only Compose project name is required")

    for variable in ("THESIS_TELEMETRY_BASE_URL", "THESIS_FARM_MCP_BASE_URL"):
        value = environment.get(variable, "")
        parsed = urlparse(value)
        if parsed.scheme != "http" or not parsed.hostname:
            raise IsolationError(f"{variable} must be an internal HTTP endpoint")
        if parsed.port in PRODUCT_HOST_PORTS:
            raise IsolationError(f"{variable} must not use a product host port")
        if parsed.hostname not in {"telemetry", "farm-mcp"}:
            raise IsolationError(f"{variable} must use the isolated Compose network")

