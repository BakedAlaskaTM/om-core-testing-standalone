"""Coordinator for repeatable Phase 1 worker-process runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from lib_dst.schemas import TraceTranscript


def run_seed_range(
    start_seed: int, count: int, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "lib_dst.worker",
            "--start-seed",
            str(start_seed),
            "--count",
            str(count),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def compare_seed_range(
    start_seed: int, count: int, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    first = run_seed_range(start_seed, count, python_hash_seed=python_hash_seed)
    second = run_seed_range(start_seed, count, python_hash_seed=python_hash_seed)
    if first != second:
        raise AssertionError(f"Worker replay mismatch: {first!r} != {second!r}")
    return first


def run_failure_in_worker(
    trace: TraceTranscript, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.worker", "--trace-stdin"],
        check=True,
        capture_output=True,
        text=True,
        input=trace.to_json(),
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_scheduled_in_worker(
    trace: TraceTranscript, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.worker", "--scheduled-trace-stdin"],
        check=True,
        capture_output=True,
        text=True,
        input=trace.to_json(),
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_candidate_in_worker(
    trace: TraceTranscript, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    """Collect candidate coverage/outcome in an isolated worker process."""
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.worker", "--candidate-stdin"],
        check=True,
        capture_output=True,
        text=True,
        input=trace.to_json(),
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_stress_in_worker(
    trace: TraceTranscript,
    *,
    axis: str,
    requested_value: int,
    python_hash_seed: str = "1",
    timeout_seconds: float | None = None,
    performance_threshold_seconds: float | None = None,
):
    """Run one Phase 4.5 point through its evidence-preserving worker boundary."""

    from lib_dst.stress import run_stress_point_in_worker

    return run_stress_point_in_worker(
        trace,
        axis=axis,
        requested_value=requested_value,
        python_hash_seed=python_hash_seed,
        timeout_seconds=timeout_seconds,
        performance_threshold_seconds=performance_threshold_seconds,
    )
