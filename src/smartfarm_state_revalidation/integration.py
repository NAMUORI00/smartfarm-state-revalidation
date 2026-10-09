from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from .adapters.http_json import get_json
from .isolation import assert_research_environment


def _hash_json(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def check_isolated_service_contracts(environment: Mapping[str, str]) -> dict[str, Any]:
    assert_research_environment(environment)
    telemetry_url = environment["THESIS_TELEMETRY_BASE_URL"]
    mcp_url = environment["THESIS_FARM_MCP_BASE_URL"]
    mcp_headers = {
        "X-Internal-Service-Token": environment["THESIS_FARM_MCP_TOKEN"],
        "X-Farm-Id": "farm-wasabi-a",
        "X-Actor-Id": "thesis-runner",
        "X-Actor-Role": "operator",
        "X-Correlation-Id": f"thesis-{environment['THESIS_RUN_ID']}",
    }

    telemetry_health = get_json(telemetry_url, "/health")
    mcp_health = get_json(mcp_url, "/health", headers=mcp_headers)
    telemetry_snapshot = get_json(
        telemetry_url,
        "/v1/telemetry/snapshot",
        query={"farm_id": "farm-wasabi-a"},
    )
    mcp_snapshot = get_json(mcp_url, "/v1/farm/snapshot", headers=mcp_headers)

    for service, health in (("telemetry", telemetry_health), ("farm-mcp", mcp_health)):
        if health.get("status") != "ok" or health.get("simulated_only") is not True:
            raise RuntimeError(f"{service} is not a healthy simulated-only service")
    telemetry_farm = telemetry_snapshot.get("farm", {})
    mcp_farm = mcp_snapshot.get("farm", {})
    if not isinstance(telemetry_farm, dict) or telemetry_farm.get("farm_id") != "farm-wasabi-a":
        raise RuntimeError("telemetry snapshot farm_id contract mismatch")
    if not isinstance(mcp_farm, dict) or mcp_farm.get("farm_id") != "farm-wasabi-a":
        raise RuntimeError("Farm MCP snapshot farm_id contract mismatch")
    if telemetry_snapshot.get("simulated_only") is not True or mcp_snapshot.get("simulated_only") is not True:
        raise RuntimeError("service snapshots must be simulated-only")
    if not isinstance(telemetry_snapshot.get("state_revision"), int):
        raise RuntimeError("telemetry snapshot state_revision contract mismatch")

    return {
        "run_id": environment["THESIS_RUN_ID"],
        "simulation_only": True,
        "compose_project": environment["THESIS_COMPOSE_PROJECT_NAME"],
        "checks": {
            "telemetry_health": True,
            "farm_mcp_health": True,
            "telemetry_snapshot": True,
            "farm_mcp_snapshot": True,
        },
        "observed": {
            "telemetry_state_revision": telemetry_snapshot["state_revision"],
            "telemetry_snapshot_sha256": _hash_json(telemetry_snapshot),
            "farm_mcp_snapshot_sha256": _hash_json(mcp_snapshot),
        },
    }
