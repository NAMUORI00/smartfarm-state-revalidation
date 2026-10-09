from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "protocols"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_sha256(path: Path) -> str:
    payload = _load(path)
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _source_sha256(path: Path) -> str:
    payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256(payload).hexdigest()


def test_confirmatory_suite_is_sealed_and_matches_frozen_hashes() -> None:
    protocol = _load(PROTOCOL_ROOT / "protocol.v1.json")
    suite_path = PROTOCOL_ROOT / "scenarios.confirmatory.v1.json"
    template_path = PROTOCOL_ROOT / "scenarios.v1.json"
    related_path = PROTOCOL_ROOT / "related-state.v1.json"
    generator_path = ROOT / "src" / "smartfarm_state_revalidation" / "scenario_generation.py"
    suite = _load(suite_path)
    frozen = protocol["frozen_inputs"]

    assert protocol["protocol_status"] == "frozen"
    assert protocol["protocol_frozen"] is True
    assert suite["suite_status"] == "frozen_confirmatory"
    assert suite["claim_status"] == "not_run"
    assert frozen["hash_serialization"] == "canonical_json_sorted_keys_utf8"
    assert frozen["generator_hash_normalization"] == "utf8_lf"
    assert _json_sha256(suite_path) == frozen["scenario_sha256"]
    assert _json_sha256(related_path) == frozen["related_state_sha256"]
    assert _json_sha256(template_path) == frozen["template_scenario_sha256"]
    assert _source_sha256(generator_path) == frozen["generator_sha256"]
    assert suite["generation"]["template_suite_sha256"] == frozen["template_scenario_sha256"]
    assert suite["generation"]["generator_sha256"] == frozen["generator_sha256"]


def test_confirmatory_sampling_units_are_unique_and_balanced() -> None:
    protocol = _load(PROTOCOL_ROOT / "protocol.v1.json")
    suite = _load(PROTOCOL_ROOT / "scenarios.confirmatory.v1.json")
    scenarios = suite["scenarios"]
    categories = Counter(item["category"] for item in scenarios)
    primary = [item for item in scenarios if item.get("comparison_scope") != "B2_safety_diagnostic"]
    diagnostics = [item for item in scenarios if item.get("comparison_scope") == "B2_safety_diagnostic"]

    assert len(scenarios) == protocol["frozen_inputs"]["scenario_count"] == 120
    assert len(primary) == protocol["frozen_inputs"]["primary_scenario_count"] == 110
    assert len(diagnostics) == protocol["frozen_inputs"]["diagnostic_scenario_count"] == 10
    assert set(categories.values()) == {10}
    assert len(categories) == 12
    assert len({item["scenario_id"] for item in scenarios}) == len(scenarios)
    assert len({item["pair_id"] for item in scenarios}) == len(scenarios)
    assert len({item["seed"] for item in scenarios}) == len(scenarios)
    assert all(item["claim_status"] == "not_run" for item in scenarios)

    pilot = _load(PROTOCOL_ROOT / "scenarios.v1.json")["scenarios"]
    assert {item["scenario_id"] for item in scenarios}.isdisjoint(
        item["scenario_id"] for item in pilot
    )
    assert {item["pair_id"] for item in scenarios}.isdisjoint(item["pair_id"] for item in pilot)


def test_confirmatory_truth_is_reconstructed_independently_from_injections() -> None:
    protocol = _load(PROTOCOL_ROOT / "protocol.v1.json")
    suite = _load(PROTOCOL_ROOT / "scenarios.confirmatory.v1.json")
    related = _load(PROTOCOL_ROOT / "related-state.v1.json")
    barriers = set(protocol["barriers"])
    registry_keys = {item["key"] for item in related["state_keys"]}
    profiles = {
        item["profile_id"]: set(item["required_state_keys"])
        for item in related["dependency_profiles"]
    }
    execution_barriers = {"approval_event", "execution_check", "action_apply"}

    for scenario in suite["scenarios"]:
        required = set(scenario["required_state_keys"])
        assert required == profiles[scenario["dependency_profile_id"]]
        assert required <= registry_keys
        release_facts = dict(scenario["initial_state"]["facts"])
        execution_facts = dict(release_facts)
        release_revision = scenario["initial_state"]["state_revision"]
        execution_revision = release_revision
        previous_revision = release_revision
        for injection in scenario["injections"]:
            assert injection["barrier"] in barriers
            assert set(injection["patch"]) <= registry_keys
            assert injection["state_revision"] > previous_revision
            previous_revision = injection["state_revision"]
            if injection["barrier"] in execution_barriers:
                execution_facts.update(injection["patch"])
                execution_revision = injection["state_revision"]
            else:
                release_facts.update(injection["patch"])
                execution_facts.update(injection["patch"])
                release_revision = injection["state_revision"]
                execution_revision = injection["state_revision"]

        release_truth = scenario["release_truth"]
        execution_truth = scenario.get("execution_truth", release_truth)
        assert release_truth["state_revision"] == release_revision
        assert execution_truth["state_revision"] == execution_revision
        assert all(release_truth["facts"][key] == release_facts[key] for key in required)
        assert all(execution_truth["facts"][key] == execution_facts[key] for key in required)


def test_confirmatory_evidence_and_action_contracts_are_complete() -> None:
    suite = _load(PROTOCOL_ROOT / "scenarios.confirmatory.v1.json")
    related = _load(PROTOCOL_ROOT / "related-state.v1.json")
    evidence = {item["evidence_set_id"]: item for item in suite["fixed_evidence_sets"]}
    registry_keys = {item["key"] for item in related["state_keys"]}
    allowed_operators = {"eq", "ne", "gt", "ge", "lt", "le"}

    for scenario in suite["scenarios"]:
        selected = evidence[scenario["fixed_evidence_set_id"]]
        assert selected["frozen_across_conditions"] is True
        assert selected["source_ids"]
        assert selected["fixture_facts"]
        contract = scenario["action_contract"]
        assert isinstance(contract["valid_action_opportunity"], bool)
        assert isinstance(contract["invalid_action_opportunity"], bool)
        assert contract["valid_action_opportunity"] or contract["invalid_action_opportunity"]
        for predicate in contract["execution_preconditions"]:
            assert predicate["state_key"] in registry_keys
            assert predicate["operator"] in allowed_operators
            assert "expected" in predicate
