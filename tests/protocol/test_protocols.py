from __future__ import annotations

import json
from pathlib import Path
import unittest


PROTOCOL_ROOT = Path(__file__).resolve().parents[2] / "protocols"


def load_json(name: str) -> dict:
    with (PROTOCOL_ROOT / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class ThesisProtocolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = load_json("protocol.v1.json")
        cls.scenario_suite = load_json("scenarios.v1.json")
        cls.related_state = load_json("related-state.v1.json")

    def test_protocol_inputs_are_not_results_and_protocol_is_frozen(self) -> None:
        for payload in (self.protocol, self.scenario_suite, self.related_state):
            self.assertEqual(payload["claim_status"], "not_run")
            self.assertEqual(payload["artifact_role"], "predeclared_experiment_input_fixture")
            self.assertIn("실험 결과", payload["fixture_notice"])
        self.assertTrue(self.protocol["protocol_frozen"])
        self.assertEqual(self.protocol["protocol_status"], "frozen")
        self.assertIsInstance(self.protocol["frozen_at"], str)
        self.assertEqual(self.protocol["sampling_and_statistics"]["repeat_count"], 1)
        self.assertEqual(self.protocol["pilot_gate"]["status"], "passed")

    def test_conditions_are_exactly_b0_b1_b2(self) -> None:
        conditions = {item["condition_id"]: item for item in self.protocol["conditions"]}
        self.assertEqual(set(conditions), {"B0", "B1", "B2"})
        self.assertEqual(conditions["B0"]["answer_refresh_limit"], 0)
        self.assertFalse(conditions["B0"]["post_generation_state_read"])
        self.assertTrue(conditions["B1"]["drop_action_on_relevant_change"])
        self.assertEqual(conditions["B2"]["answer_refresh_limit"], 1)
        self.assertEqual(
            conditions["B2"]["on_second_relevant_change"],
            "return_latest_structured_summary_and_drop_action",
        )

    def test_required_scenario_families_are_present_once(self) -> None:
        expected_categories = {
            "no_change",
            "unrelated_change",
            "noise_within_tolerance",
            "relevant_threshold_crossing",
            "alert_resolution",
            "new_alert_occurrence",
            "device_prechange",
            "conflicting_alert",
            "second_change_during_refresh",
            "stale_state",
            "change_before_execution_check",
            "change_at_action_apply",
        }
        scenarios = self.scenario_suite["scenarios"]
        self.assertEqual(len(scenarios), 12)
        self.assertEqual({item["category"] for item in scenarios}, expected_categories)
        self.assertEqual(len({item["scenario_id"] for item in scenarios}), len(scenarios))
        self.assertEqual(len({item["pair_id"] for item in scenarios}), len(scenarios))
        self.assertEqual(len({item["seed"] for item in scenarios}), len(scenarios))
        self.assertTrue(all(item["claim_status"] == "not_run" for item in scenarios))

    def test_evidence_is_frozen_and_referenced(self) -> None:
        evidence_sets = {
            item["evidence_set_id"]: item
            for item in self.scenario_suite["fixed_evidence_sets"]
        }
        self.assertTrue(evidence_sets)
        self.assertTrue(all(item["frozen_across_conditions"] for item in evidence_sets.values()))
        for scenario in self.scenario_suite["scenarios"]:
            evidence = evidence_sets[scenario["fixed_evidence_set_id"]]
            self.assertTrue(evidence["source_ids"])
            self.assertTrue(evidence["fixture_facts"])

    def test_related_state_keys_are_predeclared_and_profile_complete(self) -> None:
        registry_keys = {item["key"] for item in self.related_state["state_keys"]}
        profiles = {
            item["profile_id"]: set(item["required_state_keys"])
            for item in self.related_state["dependency_profiles"]
        }
        self.assertEqual(
            self.related_state["selection_rule"]["source"],
            "scenario_manifest_before_generation",
        )
        for scenario in self.scenario_suite["scenarios"]:
            required = set(scenario["required_state_keys"])
            self.assertEqual(required, profiles[scenario["dependency_profile_id"]])
            self.assertTrue(required <= registry_keys)
            self.assertTrue(required <= set(scenario["initial_state"]["facts"]))
            self.assertTrue(required <= set(scenario["release_truth"]["facts"]))
            if "execution_truth" in scenario:
                self.assertTrue(required <= set(scenario["execution_truth"]["facts"]))

    def test_injections_use_declared_barriers_and_registry_keys(self) -> None:
        barriers = set(self.protocol["barriers"])
        registry_keys = {item["key"] for item in self.related_state["state_keys"]}
        for scenario in self.scenario_suite["scenarios"]:
            previous_revision = scenario["initial_state"]["state_revision"]
            release_revision = previous_revision
            for injection in scenario["injections"]:
                self.assertIn(injection["barrier"], barriers)
                self.assertTrue(set(injection["patch"]) <= registry_keys)
                self.assertGreater(injection["state_revision"], previous_revision)
                previous_revision = injection["state_revision"]
                if injection["barrier"] not in {"approval_event", "execution_check", "action_apply"}:
                    release_revision = injection["state_revision"]
            self.assertEqual(release_revision, scenario["release_truth"]["state_revision"])
            if "execution_truth" in scenario:
                self.assertEqual(previous_revision, scenario["execution_truth"]["state_revision"])

    def test_release_truth_matches_ordered_patches_for_required_keys(self) -> None:
        for scenario in self.scenario_suite["scenarios"]:
            reconstructed = dict(scenario["initial_state"]["facts"])
            for injection in scenario["injections"]:
                if injection["barrier"] not in {"approval_event", "execution_check", "action_apply"}:
                    reconstructed.update(injection["patch"])
            for key in scenario["required_state_keys"]:
                self.assertEqual(
                    reconstructed[key],
                    scenario["release_truth"]["facts"][key],
                    msg=f"{scenario['scenario_id']} has inconsistent release truth for {key}",
                )
            execution_reconstructed = dict(reconstructed)
            for injection in scenario["injections"]:
                if injection["barrier"] in {"approval_event", "execution_check", "action_apply"}:
                    execution_reconstructed.update(injection["patch"])
            execution_truth = scenario.get("execution_truth", scenario["release_truth"])
            for key in scenario["required_state_keys"]:
                self.assertEqual(execution_reconstructed[key], execution_truth["facts"][key])

    def test_action_preconditions_and_opportunities_are_machine_checkable(self) -> None:
        registry_keys = {item["key"] for item in self.related_state["state_keys"]}
        allowed_operators = {"eq", "ne", "gt", "ge", "lt", "le"}
        valid_count = 0
        invalid_count = 0
        for scenario in self.scenario_suite["scenarios"]:
            contract = scenario["action_contract"]
            self.assertIsInstance(contract["valid_action_opportunity"], bool)
            self.assertIsInstance(contract["invalid_action_opportunity"], bool)
            valid_count += int(contract["valid_action_opportunity"])
            invalid_count += int(contract["invalid_action_opportunity"])
            for predicate in contract["execution_preconditions"]:
                self.assertIn(predicate["state_key"], registry_keys)
                self.assertIn(predicate["operator"], allowed_operators)
                self.assertIn("expected", predicate)
        self.assertGreater(valid_count, 0)
        self.assertGreater(invalid_count, 0)

    def test_special_cases_have_the_intended_deterministic_shape(self) -> None:
        scenarios = {
            item["scenario_id"]: item for item in self.scenario_suite["scenarios"]
        }
        unrelated = scenarios["S02-unrelated-sensor-change"]
        unrelated_patch_keys = set(unrelated["injections"][0]["patch"])
        self.assertTrue(unrelated_patch_keys.isdisjoint(unrelated["required_state_keys"]))

        noise = scenarios["S03-related-sensor-noise"]
        before = noise["initial_state"]["facts"]["sensor.zone-a.root_zone_temperature_c"]
        after = noise["release_truth"]["facts"]["sensor.zone-a.root_zone_temperature_c"]
        self.assertLessEqual(abs(after - before), 0.1)

        second_change = scenarios["S09-second-change-during-refresh"]
        self.assertEqual(len(second_change["injections"]), 2)
        self.assertEqual(
            [item["barrier"] for item in second_change["injections"]],
            ["during_initial_generation", "during_refresh_generation"],
        )
        self.assertEqual(second_change["expected_B2_bound"]["maximum_generation_calls"], 2)
        self.assertTrue(second_change["expected_B2_bound"]["action_must_be_absent"])

        stale = scenarios["S10-stale-state-at-recheck"]
        self.assertGreater(
            stale["release_truth"]["facts"]["telemetry.farm.sample_age_seconds"],
            120.0,
        )

        before_check = scenarios["S11-change-before-execution-check"]
        self.assertEqual(before_check["injections"][0]["barrier"], "approval_event")
        self.assertNotEqual(before_check["release_truth"], before_check["execution_truth"])

        at_apply = scenarios["S12-change-at-action-apply"]
        self.assertEqual(at_apply["injections"][0]["barrier"], "action_apply")
        self.assertNotEqual(at_apply["release_truth"], at_apply["execution_truth"])

    def test_oracle_metrics_statistics_and_freeze_gates_are_declared(self) -> None:
        metric_ids = {item["metric_id"] for item in self.protocol["metrics"]}
        self.assertTrue(
            {
                "response_state_consistency",
                "invalid_action_execution_rate",
                "valid_action_retention",
                "unnecessary_refresh_rate",
                "second_change_bounded_termination_rate",
            }
            <= metric_ids
        )
        self.assertLessEqual(
            set(self.protocol["claim_scope"]["claimed_metrics"]),
            metric_ids,
        )
        self.assertEqual(self.protocol["research_scope"]["judge_policy"], "no_human_or_llm_judge")
        self.assertTrue(self.protocol["oracle"]["zero_denominator"]["must_not_substitute_zero"])
        self.assertEqual(
            self.protocol["sampling_and_statistics"]["paired_key"],
            "pair_id",
        )
        self.assertEqual(
            {item["gate_id"] for item in self.protocol["gates"]},
            {
                "G0-input-contract",
                "G1-deterministic-core",
                "G2-pilot",
                "G3-freeze",
                "G4-heldout-evidence",
            },
        )


if __name__ == "__main__":
    unittest.main()
