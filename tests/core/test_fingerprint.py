from __future__ import annotations

from smartfarm_state_revalidation import StateSnapshot, compare_related, declare_related_state, fingerprint


KEY = "sensor.zone-a.root_temperature_c"
SPEC = declare_related_state(
    "root-temperature-v1",
    [KEY],
    tolerances={KEY: 0.1},
    boundaries={KEY: (18.0,)},
)


def test_noise_within_tolerance_is_not_a_relevant_change() -> None:
    before = StateSnapshot(revision=1, values={KEY: 18.20})
    after = StateSnapshot(revision=2, values={KEY: 18.25})

    diff = compare_related(before, after, SPEC)

    assert diff.changed is False
    assert fingerprint(before, SPEC) != fingerprint(after, SPEC)


def test_semantic_boundary_crossing_is_relevant_even_with_small_delta() -> None:
    before = StateSnapshot(revision=1, values={KEY: 18.05})
    after = StateSnapshot(revision=2, values={KEY: 17.95})

    diff = compare_related(before, after, SPEC)

    assert diff.changed is True
    assert diff.changes[0].reason == "boundary_crossed"


def test_global_revision_and_unrelated_values_are_not_in_related_fingerprint() -> None:
    before = StateSnapshot(revision=1, values={KEY: 18.2, "unrelated": 1})
    after = StateSnapshot(revision=99, values={KEY: 18.2, "unrelated": 2})

    assert fingerprint(before, SPEC) == fingerprint(after, SPEC)
    assert compare_related(before, after, SPEC).changed is False


def test_missing_required_key_is_a_relevant_change() -> None:
    before = StateSnapshot(revision=1, values={KEY: 18.2})
    after = StateSnapshot(revision=2, values={})

    diff = compare_related(before, after, SPEC)

    assert diff.changed is True
    assert diff.changes[0].reason == "presence_changed"


def test_declared_unit_is_part_of_the_fingerprint_contract() -> None:
    snapshot = StateSnapshot(revision=1, values={KEY: 18.2})
    celsius = declare_related_state("celsius", [KEY], units={KEY: "degC"})
    fahrenheit = declare_related_state("fahrenheit", [KEY], units={KEY: "degF"})

    assert fingerprint(snapshot, celsius) != fingerprint(snapshot, fahrenheit)
