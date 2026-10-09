from __future__ import annotations

import pytest

from smartfarm_state_revalidation import DependencyRegistry, declare_related_state


def test_registry_resolves_only_predeclared_profile() -> None:
    profile = declare_related_state("root-zone-v1", ["sensor.root_temperature"])
    registry = DependencyRegistry([profile])

    assert registry.resolve("root-zone-v1") is profile
    with pytest.raises(KeyError, match="unknown related-state profile"):
        registry.resolve("generated-from-answer")


def test_registry_rejects_duplicate_profile() -> None:
    profile = declare_related_state("root-zone-v1", ["sensor.root_temperature"])
    registry = DependencyRegistry([profile])

    with pytest.raises(ValueError, match="duplicate related-state profile"):
        registry.add(profile)


def test_profile_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="dependency keys must be unique"):
        declare_related_state("invalid", ["same", "same"])

