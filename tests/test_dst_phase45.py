from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace

import pytest

from lib_dst.corpus import CorpusStore
from lib_dst.evolution import EvolutionCoordinator
from lib_dst.failures import capture_failure
from lib_dst.faults import FaultDecision, with_faults
from lib_dst.mutation import (
    apply_mutation_decision,
    available_mutation_kinds,
    mutate_trace,
)
from lib_dst.profiles import WorkloadMetrics, WorkloadProfile, profile_names
from lib_dst.scale_generator import (
    generate_profile_trace,
    generate_stress_axis_trace,
)
from lib_dst.shrinker import shrink_failure
from lib_dst.stress import (
    StressCampaignReport,
    StressClassification,
    StressMeasurements,
    StressPointResult,
    calibrate_profiles,
    classify_worker_result,
    evaluate_stress_trace,
    run_limit_discovery,
    run_stress_default,
    run_stress_point_in_worker,
)
from lib_dst.validation import validate_trace


@pytest.mark.parametrize("profile", profile_names())
def test_profile_generation_is_byte_identical_and_within_recorded_budget(profile) -> None:
    first = generate_profile_trace(2045, profile)
    second = generate_profile_trace(2045, profile)

    assert first.to_json() == second.to_json()
    validate_trace(first)
    workload = first.metadata["workload"]
    resolved = WorkloadProfile.from_dict(workload["profile"])
    metrics = WorkloadMetrics.from_dict(workload["metrics"])
    assert not metrics.budget_violations(resolved.budgets)
    assert metrics.operations == len(first.operations)
    assert first.metadata["generator"] == "phase45.scale.v1"


def test_seed_domains_are_independent_and_materialized() -> None:
    overrides = {
        "items_per_dimension": 30,
        "populated_cells_per_cube": 20,
        "observation_samples_per_cube": 3,
    }
    first = generate_profile_trace(10, "small", overrides=overrides)
    changed = generate_profile_trace(
        10,
        "small",
        overrides=overrides,
        seed_overrides={"scale.observation": 999},
    )

    assert [operation.to_dict() for operation in first.operations] == [
        operation.to_dict() for operation in changed.operations
    ]
    assert first.observe.cells != changed.observe.cells
    assert (
        first.metadata["subseeds"]["scale.value_density"]
        == changed.metadata["subseeds"]["scale.value_density"]
    )
    assert changed.metadata["subseeds"]["scale.observation"] == f"{999:032x}"


def test_observation_sampling_always_contains_oracle_targets_and_boundaries() -> None:
    trace = generate_profile_trace(
        99,
        "small",
        overrides={
            "items_per_dimension": 40,
            "populated_cells_per_cube": 30,
            "observation_samples_per_cube": 2,
        },
    )
    sampled = {(cell.cube, cell.address) for cell in trace.observe.cells}
    oracle = trace.metadata["oracle"]
    probes = [
        {"target": oracle["target"], "expected": oracle["expected"]},
        *oracle["probes"],
    ]

    for probe in probes:
        target = probe["target"]
        assert (target["cube"], tuple(target["address"])) in sampled
    assert len(trace.observe.cells) < trace.metadata["workload"]["metrics"]["populated_cells"]


@pytest.mark.parametrize("profile", profile_names())
def test_every_profile_passes_real_engine_convergence(profile) -> None:
    trace = generate_profile_trace(77, profile)

    evidence = evaluate_stress_trace(trace)

    assert evidence["classification"]["kind"] == "pass"
    assert evidence["measurements"]["trace_bytes"] > 0
    assert evidence["measurements"]["peak_memory_bytes"] > 0


def test_scale_mutations_are_valid_recorded_and_exactly_replayable() -> None:
    trace = generate_profile_trace(8, "small")
    kinds = available_mutation_kinds(trace)

    assert {
        "scale_cardinality",
        "scale_density",
        "scale_rule",
        "scale_graph",
        "operation_burst",
    }.issubset(kinds)
    for index, kind in enumerate(kinds):
        mutated, decision = mutate_trace(trace, 88, index, kind=kind)
        replayed = apply_mutation_decision(trace, decision)
        validate_trace(mutated)
        assert replayed.to_json() == mutated.to_json()
        assert evaluate_stress_trace(mutated)["classification"]["kind"] == "pass"


def test_profile_failure_shrinks_scale_and_preserves_signature() -> None:
    trace = with_faults(
        generate_profile_trace(5, "medium"),
        [FaultDecision("persistence", "save", 1, "raise")],
    )
    original = capture_failure(trace)

    with contextlib.redirect_stdout(io.StringIO()):
        shrunk = shrink_failure(trace)

    assert shrunk.signature == original.signature
    assert len(shrunk.minimized.to_json()) < len(trace.to_json())
    assert shrunk.minimized.metadata["workload"]["metrics"]["cubes"] < trace.metadata["workload"]["metrics"]["cubes"]


def test_corpus_records_profile_strata_and_weights_them(tmp_path) -> None:
    store = CorpusStore(tmp_path / "corpus")
    small = generate_profile_trace(1, "small")
    deep = generate_profile_trace(2, "dependency_deep")
    store.add_candidate(small, ("arc:shared",), generation=0)
    store.add_candidate(deep, ("arc:shared",), generation=1)

    assert {entry.profile for entry in store.entries.values()} == {
        "small",
        "dependency_deep",
    }
    assert all(":" in entry.scale_stratum for entry in store.entries.values())
    weights = store.selection_weights(4)
    assert all(weight > 0 for weight in weights.values())


def test_evolution_accepts_profile_initializers(tmp_path) -> None:
    coordinator = EvolutionCoordinator(tmp_path / "corpus", 19)

    report = coordinator.run(
        initial_seeds=(), initial_profiles=(("small", 3),), iterations=0
    )

    assert report.initial_count == 1
    assert report.retained == 1


def test_mixed_profile_evolution_is_deterministic(tmp_path) -> None:
    initial = (("small", 1), ("dependency_deep", 2))
    first = EvolutionCoordinator(tmp_path / "first", 4545, python_hash_seed="4545")
    second = EvolutionCoordinator(tmp_path / "second", 4545, python_hash_seed="4545")

    first_report = first.run(
        initial_seeds=(), initial_profiles=initial, iterations=2
    )
    second_report = second.run(
        initial_seeds=(), initial_profiles=initial, iterations=2
    )

    assert first_report.to_json() == second_report.to_json()
    assert first.store.canonical_index_json() == second.store.canonical_index_json()


def test_parent_classification_distinguishes_break_types() -> None:
    passing_payload = evaluate_stress_trace(generate_profile_trace(1, "small"))
    classification, _, _, _ = classify_worker_result(
        returncode=0,
        stdout=json.dumps(passing_payload),
        stderr="",
        elapsed_ns=10,
        performance_threshold_ns=1,
    )
    assert classification.kind == "performance_boundary"
    assert classification.verdict == "passed"

    resource, _, _, _ = classify_worker_result(
        returncode=-1073741801, stdout="", stderr="", elapsed_ns=1
    )
    crash, _, _, _ = classify_worker_result(
        returncode=-1073741819, stdout="", stderr="", elapsed_ns=1
    )
    unknown, _, _, _ = classify_worker_result(
        returncode=17, stdout="", stderr="killed", elapsed_ns=1
    )
    malformed, _, _, _ = classify_worker_result(
        returncode=0, stdout="not-json", stderr="", elapsed_ns=1
    )

    assert resource.kind == "resource_exhaustion"
    assert crash.kind == "worker_crash"
    assert unknown.kind == "unknown_abnormal_exit"
    assert malformed.kind == "harness_error"


def test_udf_and_format_command_gaps_are_controlled_engine_errors() -> None:
    for axis, value in (("udf_depth", 1), ("format_volume", 2)):
        result = run_stress_point_in_worker(
            generate_stress_axis_trace(4, axis, value),
            axis=axis,
            requested_value=value,
        )
        assert result.classification.kind == "controlled_engine_error"
        assert result.classification.origin == "engine"
        assert result.trace_sha256 is not None


def _fake_result(trace, axis: str, value: int, *, passed_through: int = 5):
    passed = value <= passed_through
    return StressPointResult(
        axis=axis,
        requested_value=value,
        realized_value=value,
        trace_sha256=trace.sha256(),
        classification=StressClassification(
            "passed" if passed else "failed",
            "none" if passed else "engine",
            "pass" if passed else "controlled_engine_error",
            "ok" if passed else "SyntheticBoundary",
        ),
        deterministic_evidence={"synthetic": True},
        measurements=StressMeasurements(value, value, len(trace.to_json()), 0),
        environment=trace.environment.to_dict(),
        om_core_revision="test",
        raw_exit_evidence={},
    )


def test_limit_discovery_grows_refines_and_resumes_identically() -> None:
    executor = lambda trace, axis, value: _fake_result(trace, axis, value)
    complete = run_limit_discovery(
        100, "cube_count", ceiling=12, executor=executor
    )
    partial = StressCampaignReport(
        "discover:cube_count", 100, complete.points[:2]
    )
    resumed = run_limit_discovery(
        100, "cube_count", ceiling=12, executor=executor, resume=partial
    )

    assert complete.last_observed_pass == 5
    assert complete.first_observed_failure == 6
    assert complete.monotonic is True
    assert resumed.identity_json() == complete.identity_json()
    assert resumed.resumed_points == 2


def test_limit_discovery_stops_at_safety_ceiling() -> None:
    executor = lambda trace, axis, value: _fake_result(
        trace, axis, value, passed_through=100
    )
    report = run_limit_discovery(
        10, "cube_count", ceiling=2, executor=executor
    )

    assert report.last_observed_pass == 2
    assert report.points[-1].classification.kind == "safety_ceiling"
    assert report.points[-1].requested_value == 3


def test_non_monotonic_resume_reports_envelope_instead_of_exact_limit() -> None:
    trace4 = generate_stress_axis_trace(1, "cube_count", 4)
    trace8 = generate_stress_axis_trace(1, "cube_count", 8)
    failed = _fake_result(trace4, "cube_count", 4, passed_through=3)
    later_pass = _fake_result(trace8, "cube_count", 8, passed_through=8)
    resume = StressCampaignReport(
        "discover:cube_count", 1, (failed, later_pass)
    )
    executor = lambda trace, axis, value: _fake_result(
        trace, axis, value, passed_through=2
    )

    report = run_limit_discovery(
        1, "cube_count", ceiling=12, executor=executor, resume=resume
    )

    assert report.monotonic is False
    assert report.first_observed_failure == 3
    assert report.last_observed_pass == 8


def test_generation_budget_rejection_is_not_an_engine_failure() -> None:
    executor = lambda trace, axis, value: _fake_result(
        trace, axis, value, passed_through=100
    )

    report = run_limit_discovery(
        2, "dimension_count", ceiling=32, executor=executor
    )

    rejected = next(
        point for point in report.points if point.classification.kind == "generation_rejected"
    )
    assert rejected.classification.origin == "policy"
    assert rejected.trace_sha256 is None


def test_calibration_records_payload_and_seeded_shrink_cost() -> None:
    report = calibrate_profiles(42, profiles=("small",))
    point = report.points[0]

    assert point.passed
    assert point.measurements.persistence_bytes > 0
    assert point.measurements.shrink_elapsed_ns > 0
    assert point.measurements.shrink_candidates_tested > 0
    assert (
        point.deterministic_evidence["shrink_candidates_tested"]
        == point.measurements.shrink_candidates_tested
    )


def test_bounded_stress_default_repeats_excluding_volatile_measurements() -> None:
    values = (("cube_count", 2), ("rule_count", 4), ("event_volume", 4))
    first = run_stress_default(333, values=values)
    second = run_stress_default(333, values=values)

    assert first.identity_json() == second.identity_json()
    assert first.sha256() == second.sha256()
    assert all(point.passed for point in first.points)
