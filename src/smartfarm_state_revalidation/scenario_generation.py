from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _normalized_source_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _perturb_fact(key: str, value: Any, variant: int) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if key.endswith("root_zone_temperature_c"):
        return round(float(value) + variant * 0.01, 3)
    if key.endswith("relative_humidity_pct"):
        return round(float(value) + variant * 0.1, 3)
    if key.endswith("circulation_fan.percent"):
        return round(float(value) + variant, 3)
    if key == "telemetry.farm.sample_age_seconds":
        increment = variant if float(value) > 120 else variant * 0.01
        return round(float(value) + increment, 3)
    return value


def _variant_occurrences(value: Any, suffix: str) -> Any:
    if isinstance(value, dict):
        return {key: _variant_occurrences(item, suffix) for key, item in value.items()}
    if isinstance(value, list):
        return [_variant_occurrences(item, suffix) for item in value]
    if isinstance(value, str) and "occ-" in value:
        return f"{value}-{suffix}"
    return value


def _revision_map(template: Mapping[str, Any], template_index: int, variant: int) -> dict[int, int]:
    revisions = {int(template["initial_state"]["state_revision"])}
    revisions.update(int(item["state_revision"]) for item in template.get("injections", ()))
    revisions.add(int(template["release_truth"]["state_revision"]))
    if "execution_truth" in template:
        revisions.add(int(template["execution_truth"]["state_revision"]))
    base = 10_000 + template_index * 1_000 + variant * 10
    return {revision: base + index for index, revision in enumerate(sorted(revisions))}


def _remap_revisions(scenario: dict[str, Any], revision_map: Mapping[int, int]) -> None:
    scenario["initial_state"]["state_revision"] = revision_map[
        int(scenario["initial_state"]["state_revision"])
    ]
    for injection in scenario.get("injections", ()):
        injection["state_revision"] = revision_map[int(injection["state_revision"])]
    for truth_name in ("release_truth", "execution_truth"):
        if truth_name in scenario:
            truth = scenario[truth_name]
            truth["state_revision"] = revision_map[int(truth["state_revision"])]
    contract = scenario["action_contract"]
    for candidate_name in ("initial_candidate", "expected_release_candidate"):
        candidate = contract.get(candidate_name)
        if isinstance(candidate, dict) and isinstance(candidate.get("bound_state_revision"), int):
            candidate["bound_state_revision"] = revision_map[candidate["bound_state_revision"]]


def _perturb_states(scenario: dict[str, Any], variant: int) -> None:
    containers = [scenario["initial_state"]["facts"], scenario["release_truth"]["facts"]]
    if "execution_truth" in scenario:
        containers.append(scenario["execution_truth"]["facts"])
    containers.extend(item.get("patch", {}) for item in scenario.get("injections", ()))
    for facts in containers:
        for key, value in tuple(facts.items()):
            facts[key] = _perturb_fact(key, value, variant)
    for predicate in scenario["action_contract"].get("execution_preconditions", ()):
        if (
            predicate.get("operator") == "eq"
            and predicate.get("state_key", "").endswith("circulation_fan.percent")
            and isinstance(predicate.get("expected"), (int, float))
        ):
            predicate["expected"] = _perturb_fact(
                str(predicate["state_key"]), predicate["expected"], variant
            )


def generate_confirmatory_suite(
    template_suite: Mapping[str, Any],
    *,
    variants_per_template: int = 10,
    template_sha256: str | None = None,
    generator_sha256: str | None = None,
) -> dict[str, Any]:
    if variants_per_template < 1:
        raise ValueError("variants_per_template must be positive")
    generated: list[dict[str, Any]] = []
    for template_index, template in enumerate(template_suite.get("scenarios", ()), start=1):
        if not isinstance(template, Mapping):
            raise ValueError("scenario templates must be objects")
        for variant in range(1, variants_per_template + 1):
            suffix = f"h{template_index:02d}v{variant:02d}"
            scenario = _variant_occurrences(deepcopy(dict(template)), suffix)
            scenario["scenario_id"] = f"H{template_index:02d}-{variant:02d}-{template['category']}"
            scenario["pair_id"] = f"heldout-pair-{template_index:02d}-{variant:02d}"
            scenario["seed"] = 800_000 + template_index * 100 + variant
            scenario["claim_status"] = "not_run"
            _remap_revisions(scenario, _revision_map(template, template_index, variant))
            _perturb_states(scenario, variant)
            generated.append(scenario)
    return {
        "schema_version": "smartfarm-state-revalidation-confirmatory-scenarios-v1",
        "suite_id": "masters-thesis-state-revalidation-confirmatory-v1",
        "claim_status": "not_run",
        "suite_status": "frozen_confirmatory",
        "artifact_role": "predeclared_confirmatory_experiment_input",
        "fixture_notice": "모든 상태값은 동결된 모의 본 실험 입력이며 실험 결과가 아니다.",
        "condition_ids": list(template_suite.get("condition_ids", ("B0", "B1", "B2"))),
        "fixed_evidence_sets": deepcopy(list(template_suite.get("fixed_evidence_sets", ()))),
        "generation": {
            "template_suite_sha256": template_sha256,
            "generator_sha256": generator_sha256,
            "variants_per_template": variants_per_template,
            "template_count": len(template_suite.get("scenarios", ())),
            "scenario_count": len(generated),
            "numeric_perturbation": "category_preserving_deterministic_offsets",
        },
        "scenarios": generated,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the frozen confirmatory scenario suite.")
    parser.add_argument("--templates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variants", type=int, default=10)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite {args.output}")
    templates = json.loads(args.templates.read_text(encoding="utf-8"))
    suite = generate_confirmatory_suite(
        templates,
        variants_per_template=args.variants,
        template_sha256=sha256(_canonical_bytes(templates)).hexdigest(),
        generator_sha256=sha256(_normalized_source_bytes(Path(__file__))).hexdigest(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
