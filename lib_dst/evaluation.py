"""Worker-side evaluation used by the Phase 5 ``om-dst`` coordinator.

These functions run inside a clean worker process. They return canonical JSON-ready
dictionaries and never write files; the coordinator owns every artifact.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any, Iterator

from lib_dst.diff import canonical_diff
from lib_dst.failures import FailureOutcome, capture_failure
from lib_dst.generator import generate_trace
from lib_dst.scale_generator import WorkloadBudgetError, generate_profile_trace
from lib_dst.scheduling import execute_scheduled_trace
from lib_dst.schemas import EnvironmentDescriptor, TraceTranscript
from lib_dst.shrinker import shrink_failure
from lib_dst.stress import StressClassification, classify_outcome


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _diffs(outcome: FailureOutcome, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    report = outcome.report
    if report is None:
        return []
    diffs: list[dict[str, Any]] = []
    if not report.execution.oracle_passed:
        diffs.append(
            {
                "check": "oracle",
                **canonical_diff(
                    report.execution.oracle_expected,
                    report.execution.oracle_actual,
                    expected_label="oracle.expected",
                    actual_label="engine.actual",
                ),
            }
        )
    incremental = evidence.get("incremental")
    for check in report.checks:
        if check.passed or incremental is None or check.name not in evidence:
            continue
        diffs.append(
            {
                "check": check.name,
                **canonical_diff(
                    incremental,
                    evidence[check.name],
                    expected_label="incremental",
                    actual_label=check.name,
                ),
            }
        )
    return diffs


def evaluate_trace(trace: TraceTranscript) -> dict[str, Any]:
    """Execute one trace with all invariants and classify the result."""

    evidence: dict[str, Any] = {}
    outcome: FailureOutcome | None = None
    scheduled: dict[str, Any] | None = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = capture_failure(trace, evidence=evidence)
            scheduled_passed = True
            if trace.metadata.get("scheduler") is not None and not outcome.failed:
                report = execute_scheduled_trace(trace)
                scheduled_passed = report.passed
                if not scheduled_passed:
                    scheduled = report.to_dict()
        classification, deterministic = classify_outcome(outcome, scheduled_passed)
    except MemoryError:
        classification = StressClassification(
            "failed", "worker", "resource_exhaustion", "MemoryError"
        )
        deterministic = {"failure_signature": None}
    except RecursionError:
        classification = StressClassification(
            "failed", "engine", "controlled_engine_error", "RecursionError"
        )
        deterministic = {"failure_signature": None}
    except Exception as exc:  # noqa: BLE001 - classified, never hidden
        classification = StressClassification(
            "inconclusive", "worker", "worker_exception", type(exc).__name__
        )
        deterministic = {"failure_signature": None}
    return {
        "trace_sha256": trace.sha256(),
        "classification": classification.to_dict(),
        "deterministic_evidence": deterministic,
        "outcome": outcome.to_dict() if outcome is not None else None,
        "scheduled": scheduled,
        "diffs": _diffs(outcome, evidence) if outcome is not None else [],
        "worker_environment": EnvironmentDescriptor.current().to_dict(),
    }


def generate_seed_trace(seed: int, profile: str | None = None) -> TraceTranscript:
    if profile is None:
        return generate_trace(seed)
    return generate_profile_trace(seed, profile)


def iter_seed_outcomes(
    start_seed: int, count: int, profile: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield one result per seed. Non-passing results carry their standalone trace."""

    if count < 1:
        raise ValueError("count must be positive")
    for seed in range(start_seed, start_seed + count):
        try:
            trace = generate_seed_trace(seed, profile)
        except WorkloadBudgetError as exc:
            yield {
                "seed": seed,
                "trace_sha256": None,
                "classification": StressClassification(
                    "inconclusive", "policy", "generation_rejected", "budget-exceeded"
                ).to_dict(),
                "deterministic_evidence": {"violations": list(exc.violations)},
            }
            continue
        except Exception as exc:  # noqa: BLE001
            yield {
                "seed": seed,
                "trace_sha256": None,
                "classification": StressClassification(
                    "inconclusive", "harness", "harness_error", type(exc).__name__
                ).to_dict(),
                "deterministic_evidence": {},
            }
            continue
        result = evaluate_trace(trace)
        line: dict[str, Any] = {
            "seed": seed,
            "trace_sha256": result["trace_sha256"],
            "classification": result["classification"],
            "deterministic_evidence": result["deterministic_evidence"],
        }
        if result["classification"]["verdict"] != "passed":
            line.update(
                outcome=result["outcome"],
                scheduled=result["scheduled"],
                diffs=result["diffs"],
                worker_environment=result["worker_environment"],
                trace_json=trace.to_json(),
            )
        yield line


def shrink_trace(trace: TraceTranscript) -> dict[str, Any]:
    """Shrink a failing trace in this worker. Passing traces are reported, not raised."""

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = shrink_failure(trace)
    except ValueError as exc:
        return {"status": "not-failing", "original_sha256": trace.sha256(), "detail": str(exc)}
    return {
        "status": "minimized",
        "original_sha256": result.original_sha256,
        "minimized_sha256": result.minimized.sha256(),
        "signature": result.signature.to_dict(),
        "candidates_tested": result.candidates_tested,
        "minimized_json": result.minimized.to_json(),
    }


def emit(value: dict[str, Any]) -> None:
    print(_dumps(value), flush=True)
