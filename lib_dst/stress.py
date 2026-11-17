"""Deterministic stress campaigns and evidence-based break classification."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import time
import tracemalloc
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from lib_dst.failures import FailureOutcome, capture_failure
from lib_dst.faults import FaultDecision, with_faults
from lib_dst.invariants import PERSISTENCE_PATH
from lib_dst.profiles import profile_names
from lib_dst.provenance import om_core_revision
from lib_dst.scale_generator import (
    WorkloadBudgetError,
    generate_profile_trace,
    generate_stress_axis_trace,
)
from lib_dst.scheduling import execute_scheduled_trace
from lib_dst.schemas import EnvironmentDescriptor, TraceTranscript
from lib_dst.shrinker import shrink_failure
from lib_storeadapters.memory_adapters import InMemoryJsonAdapter


STRESS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StressClassification:
    verdict: str
    origin: str
    kind: str
    code: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "verdict": self.verdict,
            "origin": self.origin,
            "kind": self.kind,
            "code": self.code,
        }
        if self.detail is not None:
            value["detail"] = self.detail
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StressClassification":
        return cls(
            verdict=str(value["verdict"]),
            origin=str(value["origin"]),
            kind=str(value["kind"]),
            code=str(value["code"]),
            detail=value.get("detail"),
        )


@dataclass(frozen=True)
class StressMeasurements:
    elapsed_ns: int
    peak_memory_bytes: int
    trace_bytes: int
    report_bytes: int
    persistence_bytes: int = 0
    shrink_elapsed_ns: int = 0
    shrink_candidates_tested: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "elapsed_ns": self.elapsed_ns,
            "peak_memory_bytes": self.peak_memory_bytes,
            "trace_bytes": self.trace_bytes,
            "report_bytes": self.report_bytes,
            "persistence_bytes": self.persistence_bytes,
            "shrink_elapsed_ns": self.shrink_elapsed_ns,
            "shrink_candidates_tested": self.shrink_candidates_tested,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StressMeasurements":
        return cls(**{key: int(value.get(key, 0)) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class StressPointResult:
    axis: str
    requested_value: int
    realized_value: int | None
    trace_sha256: str | None
    classification: StressClassification
    deterministic_evidence: dict[str, Any]
    measurements: StressMeasurements
    environment: dict[str, Any]
    om_core_revision: str
    raw_exit_evidence: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.classification.verdict == "passed"

    def identity_dict(self) -> dict[str, Any]:
        # The OM Core revision is provenance, not identity: including it would change
        # every report hash on every commit even when all results are identical.
        return {
            "axis": self.axis,
            "requested_value": self.requested_value,
            "realized_value": self.realized_value,
            "trace_sha256": self.trace_sha256,
            "classification": self.classification.to_dict(),
            "deterministic_evidence": self.deterministic_evidence,
            "environment": self.environment,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_dict()
        value["om_core_revision"] = self.om_core_revision
        value["measurements"] = self.measurements.to_dict()
        value["raw_exit_evidence"] = self.raw_exit_evidence
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StressPointResult":
        return cls(
            axis=str(value["axis"]),
            requested_value=int(value["requested_value"]),
            realized_value=(
                int(value["realized_value"])
                if value.get("realized_value") is not None
                else None
            ),
            trace_sha256=value.get("trace_sha256"),
            classification=StressClassification.from_dict(value["classification"]),
            deterministic_evidence=dict(value.get("deterministic_evidence", {})),
            measurements=StressMeasurements.from_dict(value.get("measurements", {})),
            environment=dict(value.get("environment", {})),
            om_core_revision=str(value.get("om_core_revision", "unknown")),
            raw_exit_evidence=dict(value.get("raw_exit_evidence", {})),
        )


@dataclass(frozen=True)
class StressCampaignReport:
    preset: str
    master_seed: int | str
    points: tuple[StressPointResult, ...]
    last_observed_pass: int | None = None
    first_observed_failure: int | None = None
    monotonic: bool | None = None
    resumed_points: int = 0
    schema_version: int = STRESS_SCHEMA_VERSION

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preset": self.preset,
            "master_seed": self.master_seed,
            "points": [point.identity_dict() for point in self.points],
            "last_observed_pass": self.last_observed_pass,
            "first_observed_failure": self.first_observed_failure,
            "monotonic": self.monotonic,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_dict()
        value["points"] = [point.to_dict() for point in self.points]
        value["resumed_points"] = self.resumed_points
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def identity_json(self) -> str:
        return json.dumps(self.identity_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.identity_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StressCampaignReport":
        return cls(
            schema_version=int(value.get("schema_version", STRESS_SCHEMA_VERSION)),
            preset=str(value["preset"]),
            master_seed=value["master_seed"],
            points=tuple(StressPointResult.from_dict(point) for point in value.get("points", ())),
            last_observed_pass=value.get("last_observed_pass"),
            first_observed_failure=value.get("first_observed_failure"),
            monotonic=value.get("monotonic"),
            resumed_points=int(value.get("resumed_points", 0)),
        )


@dataclass(frozen=True)
class StressAxis:
    name: str
    start: int
    ceiling: int
    growth_factor: int = 2


STRESS_AXES: dict[str, StressAxis] = {
    axis.name: axis
    for axis in (
        StressAxis("trace_ast_depth", 1, 64),
        StressAxis("builtin_nesting", 1, 128),
        StressAxis("expression_width", 1, 128),
        StressAxis("udf_depth", 1, 16),
        StressAxis("cube_count", 1, 16),
        StressAxis("rule_count", 1, 64),
        StressAxis("dimension_count", 1, 8),
        StressAxis("dimensions_per_cube", 1, 4),
        StressAxis("items_per_dimension", 2, 64),
        StressAxis("hierarchy_depth", 1, 16),
        StressAxis("hierarchy_breadth", 2, 64),
        StressAxis("dependency_depth", 1, 16),
        StressAxis("dependency_fanout", 1, 16),
        StressAxis("populated_cells", 1, 64),
        StressAxis("persistence_cycles", 1, 8),
        StressAxis("event_volume", 1, 128),
        StressAxis("recalculation_churn", 1, 128),
        StressAxis("view_count", 1, 8),
        StressAxis("format_volume", 1, 64),
        StressAxis("model_churn", 1, 32),
    )
}


STRESS_DEFAULT_VALUES: tuple[tuple[str, int], ...] = (
    ("trace_ast_depth", 12),
    ("builtin_nesting", 24),
    ("expression_width", 32),
    ("udf_depth", 8),
    ("cube_count", 12),
    ("rule_count", 32),
    ("dimension_count", 6),
    ("dimensions_per_cube", 4),
    ("items_per_dimension", 48),
    ("hierarchy_depth", 8),
    ("hierarchy_breadth", 32),
    ("dependency_depth", 12),
    ("dependency_fanout", 8),
    ("populated_cells", 48),
    ("persistence_cycles", 4),
    ("event_volume", 64),
    ("recalculation_churn", 32),
    ("view_count", 4),
    ("format_volume", 24),
    ("model_churn", 12),
)


def classify_outcome(
    outcome: FailureOutcome, scheduled_passed: bool = True
) -> tuple[StressClassification, dict[str, Any]]:
    """Map a normalized worker outcome onto the verdict/origin/kind classification."""

    if not outcome.failed and scheduled_passed:
        classification = StressClassification("passed", "none", "pass", "ok")
        return classification, {"failure_signature": None, "scheduled_passed": True}
    if not scheduled_passed:
        classification = StressClassification(
            "failed", "engine", "correctness_mismatch", "scheduled-invariant"
        )
        return classification, {"failure_signature": None, "scheduled_passed": False}
    assert outcome.signature is not None
    signature = outcome.signature.to_dict()
    category = signature["category"]
    if category == "invariant":
        classification = StressClassification(
            "failed", "engine", "correctness_mismatch", signature["code"]
        )
    elif category in {"runtime", "persistence"}:
        classification = StressClassification(
            "failed", "engine", "controlled_engine_error", signature["code"]
        )
    elif category == "unexpected" and signature["code"] in {
        "RuntimeError",
        "RecursionError",
        "RuleValidationError",
    }:
        classification = StressClassification(
            "failed", "engine", "controlled_engine_error", signature["code"]
        )
    elif category == "validation":
        classification = StressClassification(
            "inconclusive", "harness", "harness_error", signature["code"]
        )
    else:
        classification = StressClassification(
            "inconclusive", "worker", "worker_exception", signature["code"]
        )
    return classification, {"failure_signature": signature, "scheduled_passed": scheduled_passed}


def evaluate_stress_trace(trace: TraceTranscript) -> dict[str, Any]:
    """Worker-side execution. All expected failures are returned as JSON."""

    started = time.perf_counter_ns()
    tracemalloc.start()
    report_payload = ""
    persistence_bytes = 0
    storage = InMemoryJsonAdapter()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = capture_failure(trace, workspace_adapter=storage)
            scheduled_passed = True
            if trace.metadata.get("scheduler") is not None and not outcome.failed:
                scheduled_passed = execute_scheduled_trace(trace).passed
        classification, evidence = classify_outcome(outcome, scheduled_passed)
        report_payload = outcome.to_json()
        try:
            persistence_bytes = len(storage.payload(PERSISTENCE_PATH).encode("utf-8"))
        except KeyError:
            persistence_bytes = 0
    except MemoryError:
        classification = StressClassification(
            "failed", "worker", "resource_exhaustion", "MemoryError"
        )
        evidence = {"failure_signature": None}
    except RecursionError:
        classification = StressClassification(
            "failed", "engine", "controlled_engine_error", "RecursionError"
        )
        evidence = {"failure_signature": None}
    except Exception as exc:
        classification = StressClassification(
            "inconclusive", "worker", "worker_exception", type(exc).__name__
        )
        evidence = {"failure_signature": None}
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    elapsed = time.perf_counter_ns() - started
    return {
        "classification": classification.to_dict(),
        "deterministic_evidence": evidence,
        "measurements": StressMeasurements(
            elapsed_ns=elapsed,
            peak_memory_bytes=peak,
            trace_bytes=len(trace.to_json().encode("utf-8")),
            report_bytes=len(report_payload.encode("utf-8")),
            persistence_bytes=persistence_bytes,
        ).to_dict(),
    }


# Windows reports NTSTATUS exit codes as unsigned 32-bit values (for example
# 3221225495 for 0xC0000017). Older callers and tests used the signed form; both are
# normalized with ``& 0xFFFFFFFF`` before lookup.
_WINDOWS_RESOURCE_CODES = {
    0xC0000017,  # STATUS_NO_MEMORY
    0xC00000FD,  # STATUS_STACK_OVERFLOW
}
_WINDOWS_CRASH_CODES = {
    0xC0000005,  # STATUS_ACCESS_VIOLATION
    0xC000001D,  # STATUS_ILLEGAL_INSTRUCTION
    0xC0000409,  # STATUS_STACK_BUFFER_OVERRUN
}
# POSIX children killed by a signal have a negative return code. SIGKILL is deliberately
# absent: the kernel OOM killer and an external kill are indistinguishable from it.
_POSIX_CRASH_SIGNALS = {
    4: "SIGILL",
    6: "SIGABRT",
    7: "SIGBUS",
    8: "SIGFPE",
    11: "SIGSEGV",
}
_POSIX_RESOURCE_SIGNALS = {
    24: "SIGXCPU",
    25: "SIGXFSZ",
}


def classify_exit_status(returncode: int) -> StressClassification:
    """Classify a nonzero child exit status from positive platform evidence only."""

    if -64 <= returncode < 0:
        number = -returncode
        if number in _POSIX_CRASH_SIGNALS:
            return StressClassification(
                "failed", "worker", "worker_crash", f"signal:{_POSIX_CRASH_SIGNALS[number]}"
            )
        if number in _POSIX_RESOURCE_SIGNALS:
            return StressClassification(
                "failed",
                "os",
                "resource_exhaustion",
                f"signal:{_POSIX_RESOURCE_SIGNALS[number]}",
            )
        return StressClassification(
            "inconclusive", "os", "unknown_abnormal_exit", f"signal:{number}"
        )
    status = returncode & 0xFFFFFFFF
    if status in _WINDOWS_RESOURCE_CODES:
        return StressClassification(
            "failed", "os", "resource_exhaustion", f"ntstatus:0x{status:08X}"
        )
    if status in _WINDOWS_CRASH_CODES:
        return StressClassification(
            "failed", "worker", "worker_crash", f"ntstatus:0x{status:08X}"
        )
    return StressClassification(
        "inconclusive", "os", "unknown_abnormal_exit", f"exit:{returncode}"
    )


def timeout_classification() -> StressClassification:
    """A coordinator-enforced wall-clock limit is positive, host-dependent evidence."""

    return StressClassification(
        "failed",
        "policy",
        "resource_exhaustion",
        "wall-timeout",
        detail="host-dependent: enforced by the coordinator's wall-clock limit",
    )


def classify_worker_result(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    elapsed_ns: int,
    performance_threshold_ns: int | None = None,
) -> tuple[StressClassification, dict[str, Any], StressMeasurements, dict[str, Any]]:
    """Classify parent-observed child evidence without guessing a cause."""

    raw = {
        "returncode": returncode,
        "stderr_tail": stderr[-2000:],
        "stdout_line_count": len(stdout.splitlines()),
    }
    if returncode != 0:
        classification = classify_exit_status(returncode)
        return classification, {}, StressMeasurements(elapsed_ns, 0, 0, 0), raw

    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
        classification = StressClassification.from_dict(payload["classification"])
        measurements = StressMeasurements.from_dict(payload["measurements"])
        evidence = dict(payload.get("deterministic_evidence", {}))
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        classification = StressClassification(
            "inconclusive", "harness", "harness_error", type(exc).__name__
        )
        return classification, {}, StressMeasurements(elapsed_ns, 0, 0, 0), raw

    if (
        classification.verdict == "passed"
        and performance_threshold_ns is not None
        and elapsed_ns > performance_threshold_ns
    ):
        classification = StressClassification(
            "passed", "policy", "performance_boundary", "threshold-exceeded"
        )
    measurements = replace(measurements, elapsed_ns=elapsed_ns)
    return classification, evidence, measurements, raw


def run_stress_point_in_worker(
    trace: TraceTranscript,
    *,
    axis: str,
    requested_value: int,
    python_hash_seed: str = "1",
    timeout_seconds: float | None = None,
    performance_threshold_seconds: float | None = None,
    memory_mb: int | None = None,
) -> StressPointResult:
    from lib_dst.process import WorkerLimits, run_worker

    completion = run_worker(
        ["--stress-point-stdin"],
        stdin=trace.to_json(),
        limits=WorkerLimits(timeout_seconds=timeout_seconds, memory_mb=memory_mb),
        python_hash_seed=python_hash_seed,
    )
    if completion.timed_out:
        return _point_result(
            trace,
            axis,
            requested_value,
            timeout_classification(),
            {},
            StressMeasurements(
                completion.elapsed_ns, 0, len(trace.to_json().encode("utf-8")), 0
            ),
            {
                **completion.limit_evidence(),
                "stdout": completion.stdout[-1000:],
                "stderr_tail": completion.stderr[-2000:],
            },
        )
    assert completion.returncode is not None
    classification, evidence, measurements, raw = classify_worker_result(
        returncode=completion.returncode,
        stdout=completion.stdout,
        stderr=completion.stderr,
        elapsed_ns=completion.elapsed_ns,
        performance_threshold_ns=(
            int(performance_threshold_seconds * 1_000_000_000)
            if performance_threshold_seconds is not None
            else None
        ),
    )
    raw = {**raw, **completion.limit_evidence()}
    return _point_result(
        trace,
        axis,
        requested_value,
        classification,
        evidence,
        measurements,
        raw,
    )


def _point_result(
    trace: TraceTranscript,
    axis: str,
    requested_value: int,
    classification: StressClassification,
    evidence: dict[str, Any],
    measurements: StressMeasurements,
    raw: dict[str, Any],
) -> StressPointResult:
    workload = trace.metadata.get("workload", {})
    return StressPointResult(
        axis=axis,
        requested_value=requested_value,
        realized_value=workload.get("stress_value", requested_value),
        trace_sha256=trace.sha256(),
        classification=classification,
        deterministic_evidence=evidence,
        measurements=measurements,
        environment=trace.environment.to_dict(),
        om_core_revision=om_core_revision(),
        raw_exit_evidence=raw,
    )


def _synthetic_point(
    axis: str,
    value: int,
    classification: StressClassification,
    *,
    seed: int | str,
    evidence: dict[str, Any] | None = None,
) -> StressPointResult:
    environment = EnvironmentDescriptor.current().to_dict()
    return StressPointResult(
        axis,
        value,
        None,
        None,
        classification,
        evidence or {},
        StressMeasurements(0, 0, 0, 0),
        environment,
        om_core_revision(),
        {},
    )


PointExecutor = Callable[[TraceTranscript, str, int], StressPointResult]


def _default_executor(trace: TraceTranscript, axis: str, value: int) -> StressPointResult:
    return run_stress_point_in_worker(trace, axis=axis, requested_value=value)


def run_stress_default(
    master_seed: int | str,
    *,
    executor: PointExecutor | None = None,
    values: Iterable[tuple[str, int]] = STRESS_DEFAULT_VALUES,
) -> StressCampaignReport:
    execute = executor or _default_executor
    points = []
    for axis, value in values:
        trace = generate_stress_axis_trace(master_seed, axis, value)
        points.append(execute(trace, axis, value))
    return StressCampaignReport("stress_default", master_seed, tuple(points))


def run_limit_discovery(
    master_seed: int | str,
    axis: str,
    *,
    ceiling: int | None = None,
    executor: PointExecutor | None = None,
    resume: StressCampaignReport | None = None,
) -> StressCampaignReport:
    try:
        definition = STRESS_AXES[axis]
    except KeyError as exc:
        raise ValueError(f"Unknown stress axis: {axis}") from exc
    effective_ceiling = int(ceiling if ceiling is not None else definition.ceiling)
    if effective_ceiling < definition.start:
        raise ValueError("Stress ceiling cannot be below the starting value")
    execute = executor or _default_executor
    points = list(resume.points if resume is not None else ())
    by_value = {point.requested_value: point for point in points}
    resumed = len(points)

    def evaluate(value: int) -> StressPointResult:
        if value in by_value:
            return by_value[value]
        try:
            trace = generate_stress_axis_trace(master_seed, axis, value)
        except WorkloadBudgetError as exc:
            point = _synthetic_point(
                axis,
                value,
                StressClassification(
                    "inconclusive", "policy", "generation_rejected", "budget-exceeded"
                ),
                seed=master_seed,
                evidence={"violations": list(exc.violations)},
            )
        except Exception as exc:
            point = _synthetic_point(
                axis,
                value,
                StressClassification(
                    "inconclusive", "harness", "harness_error", type(exc).__name__
                ),
                seed=master_seed,
            )
        else:
            point = execute(trace, axis, value)
        by_value[value] = point
        points.append(point)
        return point

    value = definition.start
    last_pass: int | None = None
    first_failure: int | None = None
    while value <= effective_ceiling:
        point = evaluate(value)
        if point.passed:
            last_pass = value
            next_value = value * definition.growth_factor
            if next_value == value:
                next_value += 1
            value = next_value
            continue
        first_failure = value
        break

    if first_failure is None and (last_pass or 0) < effective_ceiling:
        ceiling_point = evaluate(effective_ceiling)
        if ceiling_point.passed:
            last_pass = effective_ceiling
        else:
            first_failure = effective_ceiling

    if first_failure is None:
        next_value = effective_ceiling + 1
        points.append(
            _synthetic_point(
                axis,
                next_value,
                StressClassification(
                    "inconclusive", "policy", "safety_ceiling", "configured-ceiling"
                ),
                seed=master_seed,
                evidence={"ceiling": effective_ceiling},
            )
        )
    elif last_pass is not None and first_failure - last_pass > 1:
        lower, upper = last_pass, first_failure
        while upper - lower > 1:
            midpoint = (lower + upper) // 2
            point = evaluate(midpoint)
            if point.passed:
                lower = midpoint
            else:
                upper = midpoint
        last_pass, first_failure = lower, upper

    ordered = tuple(sorted(points, key=lambda point: point.requested_value))
    failures = [point.requested_value for point in ordered if point.classification.verdict == "failed"]
    passes = [point.requested_value for point in ordered if point.passed]
    monotonic = not failures or not any(value > min(failures) for value in passes)
    return StressCampaignReport(
        f"discover:{axis}",
        master_seed,
        ordered,
        last_observed_pass=max(passes) if passes else None,
        first_observed_failure=min(failures) if failures else first_failure,
        monotonic=monotonic,
        resumed_points=resumed,
    )


def calibrate_profiles(
    master_seed: int | str,
    *,
    profiles: Iterable[str] | None = None,
    executor: PointExecutor | None = None,
    measure_shrink: bool = True,
) -> StressCampaignReport:
    execute = executor or _default_executor
    points: list[StressPointResult] = []
    for index, profile in enumerate(profiles or profile_names(), start=1):
        trace = generate_profile_trace(master_seed, profile)
        point = execute(trace, f"profile:{profile}", index)
        if measure_shrink:
            failing = with_faults(
                trace,
                [FaultDecision("persistence", "save", 1, "raise")],
            )
            started = time.perf_counter_ns()
            with contextlib.redirect_stdout(io.StringIO()):
                shrunk = shrink_failure(failing)
            shrink_elapsed = time.perf_counter_ns() - started
            evidence = dict(point.deterministic_evidence)
            evidence["shrink_candidates_tested"] = shrunk.candidates_tested
            point = replace(
                point,
                deterministic_evidence=evidence,
                measurements=replace(
                    point.measurements,
                    shrink_elapsed_ns=shrink_elapsed,
                    shrink_candidates_tested=shrunk.candidates_tested,
                ),
            )
        points.append(point)
    return StressCampaignReport("calibration", master_seed, tuple(points))
