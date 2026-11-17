"""Resolved deterministic workload profiles for Phase 4.5."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StructuralBudgets:
    max_dimensions: int
    max_items_per_dimension: int
    max_addressable_cells: int
    max_populated_cells: int
    max_cubes: int
    max_rules: int
    max_ast_nodes: int
    max_dependency_edges: int
    max_operations: int
    max_persistence_bytes: int
    max_virtual_events: int

    def __post_init__(self) -> None:
        if any(value < 1 for value in asdict(self).values()):
            raise ValueError("Structural budgets must be positive")

    def to_dict(self) -> dict[str, int]:
        return {key: int(value) for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StructuralBudgets":
        return cls(**{name: int(value[name]) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    dimensions: int
    items_per_dimension: int
    cubes: int
    dimensions_per_cube: int
    rules_per_cube: int
    populated_cells_per_cube: int
    ast_depth: int
    expression_width: int
    dependency_depth: int
    dependency_fanout: int
    udf_depth: int
    hierarchy_depth: int
    views_per_cube: int
    format_cells_per_cube: int
    model_churn: int
    persistence_cycles: int
    event_recalculations: int
    observation_samples_per_cube: int
    budgets: StructuralBudgets
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported workload profile schema: {self.schema_version}")
        numeric = {
            key: value
            for key, value in asdict(self).items()
            if key not in {"name", "budgets", "schema_version"}
        }
        if any(int(value) < 0 for value in numeric.values()):
            raise ValueError("Workload profile values cannot be negative")
        if not self.name:
            raise ValueError("Workload profile name cannot be empty")
        if self.dimensions < 1 or self.cubes < 1 or self.items_per_dimension < 2:
            raise ValueError("A workload requires dimensions, cubes, and at least two items")
        if not 1 <= self.dimensions_per_cube <= self.dimensions:
            raise ValueError("dimensions_per_cube must be within the dimension count")

    def with_values(self, **changes: int | str | StructuralBudgets) -> "WorkloadProfile":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["budgets"] = self.budgets.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkloadProfile":
        fields = {
            name: value[name]
            for name in cls.__dataclass_fields__
            if name not in {"budgets", "schema_version"}
        }
        return cls(
            **fields,
            budgets=StructuralBudgets.from_dict(value["budgets"]),
            schema_version=int(value.get("schema_version", PROFILE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class WorkloadMetrics:
    dimensions: int
    items: int
    max_items_per_dimension: int
    addressable_cells: int
    populated_cells: int
    cubes: int
    rules: int
    ast_nodes: int
    dependency_edges: int
    operations: int
    estimated_persistence_bytes: int
    virtual_events: int

    def to_dict(self) -> dict[str, int]:
        return {key: int(value) for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkloadMetrics":
        return cls(**{name: int(value[name]) for name in cls.__dataclass_fields__})

    def budget_violations(self, budgets: StructuralBudgets) -> tuple[str, ...]:
        checks = {
            "dimensions": (self.dimensions, budgets.max_dimensions),
            "max_items_per_dimension": (
                self.max_items_per_dimension,
                budgets.max_items_per_dimension,
            ),
            "addressable_cells": (
                self.addressable_cells,
                budgets.max_addressable_cells,
            ),
            "populated_cells": (self.populated_cells, budgets.max_populated_cells),
            "cubes": (self.cubes, budgets.max_cubes),
            "rules": (self.rules, budgets.max_rules),
            "ast_nodes": (self.ast_nodes, budgets.max_ast_nodes),
            "dependency_edges": (
                self.dependency_edges,
                budgets.max_dependency_edges,
            ),
            "operations": (self.operations, budgets.max_operations),
            "estimated_persistence_bytes": (
                self.estimated_persistence_bytes,
                budgets.max_persistence_bytes,
            ),
            "virtual_events": (self.virtual_events, budgets.max_virtual_events),
        }
        return tuple(
            f"{name}:{actual}>{limit}"
            for name, (actual, limit) in checks.items()
            if actual > limit
        )


_STANDARD_BUDGETS = StructuralBudgets(
    max_dimensions=16,
    max_items_per_dimension=256,
    max_addressable_cells=10_000_000,
    max_populated_cells=100_000,
    max_cubes=64,
    max_rules=10_000,
    max_ast_nodes=100_000,
    max_dependency_edges=100_000,
    max_operations=250_000,
    max_persistence_bytes=256 * 1024 * 1024,
    max_virtual_events=250_000,
)


def _profile(name: str, **changes: int) -> WorkloadProfile:
    base: dict[str, Any] = {
        "name": name,
        "dimensions": 1,
        "items_per_dimension": 6,
        "cubes": 1,
        "dimensions_per_cube": 1,
        "rules_per_cube": 2,
        "populated_cells_per_cube": 2,
        "ast_depth": 2,
        "expression_width": 3,
        "dependency_depth": 0,
        "dependency_fanout": 1,
        "udf_depth": 0,
        "hierarchy_depth": 0,
        "views_per_cube": 0,
        "format_cells_per_cube": 0,
        "model_churn": 0,
        "persistence_cycles": 1,
        "event_recalculations": 1,
        "observation_samples_per_cube": 8,
        "budgets": _STANDARD_BUDGETS,
    }
    base.update(changes)
    return WorkloadProfile(**base)


DEFAULT_PROFILES: dict[str, WorkloadProfile] = {
    "small": _profile("small"),
    "medium": _profile(
        "medium",
        dimensions=3,
        items_per_dimension=8,
        cubes=3,
        dimensions_per_cube=2,
        rules_per_cube=3,
        populated_cells_per_cube=4,
        ast_depth=3,
        expression_width=6,
        dependency_depth=2,
        dependency_fanout=2,
        hierarchy_depth=2,
        views_per_cube=1,
        model_churn=2,
        observation_samples_per_cube=10,
    ),
    "large": _profile(
        "large",
        dimensions=4,
        items_per_dimension=12,
        cubes=6,
        dimensions_per_cube=3,
        rules_per_cube=5,
        populated_cells_per_cube=8,
        ast_depth=4,
        expression_width=12,
        dependency_depth=4,
        dependency_fanout=3,
        udf_depth=0,
        hierarchy_depth=3,
        views_per_cube=1,
        persistence_cycles=2,
        event_recalculations=3,
        observation_samples_per_cube=16,
    ),
    "dependency_deep": _profile(
        "dependency_deep",
        dimensions=1,
        items_per_dimension=18,
        cubes=8,
        rules_per_cube=2,
        populated_cells_per_cube=2,
        ast_depth=5,
        expression_width=8,
        dependency_depth=8,
        dependency_fanout=3,
        udf_depth=0,
        observation_samples_per_cube=8,
    ),
    "persistence_heavy": _profile(
        "persistence_heavy",
        dimensions=3,
        items_per_dimension=16,
        cubes=5,
        dimensions_per_cube=2,
        rules_per_cube=5,
        populated_cells_per_cube=12,
        ast_depth=3,
        dependency_depth=3,
        hierarchy_depth=3,
        views_per_cube=2,
        model_churn=2,
        persistence_cycles=4,
        observation_samples_per_cube=20,
    ),
    "event_heavy": _profile(
        "event_heavy",
        dimensions=2,
        items_per_dimension=8,
        cubes=2,
        dimensions_per_cube=2,
        rules_per_cube=3,
        populated_cells_per_cube=4,
        ast_depth=2,
        event_recalculations=40,
        observation_samples_per_cube=10,
    ),
}


def resolve_profile(
    profile: str | WorkloadProfile,
    *,
    overrides: Mapping[str, int] | None = None,
) -> WorkloadProfile:
    if isinstance(profile, WorkloadProfile):
        resolved = profile
    else:
        try:
            resolved = DEFAULT_PROFILES[profile]
        except KeyError as exc:
            raise ValueError(f"Unknown workload profile: {profile}") from exc
    if overrides:
        unknown = set(overrides) - set(WorkloadProfile.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown workload override(s): {sorted(unknown)}")
        resolved = resolved.with_values(**{key: int(value) for key, value in overrides.items()})
    return resolved


def profile_names() -> tuple[str, ...]:
    return tuple(DEFAULT_PROFILES)
