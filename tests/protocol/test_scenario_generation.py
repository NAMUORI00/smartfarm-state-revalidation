from __future__ import annotations

import json
from pathlib import Path

from smartfarm_state_revalidation.scenario_generation import generate_confirmatory_suite


ROOT = Path(__file__).resolve().parents[2]


def test_confirmatory_generation_creates_unique_category_preserving_instances() -> None:
    templates = json.loads((ROOT / "protocols" / "scenarios.v1.json").read_text(encoding="utf-8"))
    suite = generate_confirmatory_suite(templates, variants_per_template=10)
    scenarios = suite["scenarios"]

    assert suite["suite_status"] == "frozen_confirmatory"
    assert suite["claim_status"] == "not_run"
    assert len(scenarios) == 120
    assert len({item["scenario_id"] for item in scenarios}) == 120
    assert len({item["pair_id"] for item in scenarios}) == 120
    assert len({item["seed"] for item in scenarios}) == 120
    assert sum(item["comparison_scope"] == "paired_primary" for item in scenarios) == 110
    assert sum(item["comparison_scope"] == "B2_safety_diagnostic" for item in scenarios) == 10


def test_confirmatory_instances_keep_truth_and_revision_contracts() -> None:
    templates = json.loads((ROOT / "protocols" / "scenarios.v1.json").read_text(encoding="utf-8"))
    suite = generate_confirmatory_suite(templates, variants_per_template=3)

    for scenario in suite["scenarios"]:
        revisions = [scenario["initial_state"]["state_revision"]]
        revisions.extend(item["state_revision"] for item in scenario["injections"])
        assert revisions == sorted(set(revisions))
        assert set(scenario["required_state_keys"]) <= set(scenario["release_truth"]["facts"])
        execution_truth = scenario.get("execution_truth", scenario["release_truth"])
        assert set(scenario["required_state_keys"]) <= set(execution_truth["facts"])
        assert scenario["claim_status"] == "not_run"
