"""Evidence-preserving clean-worker subprocess execution.

Unlike the Phase 1-4 runner helpers, ``run_worker`` never raises for an abnormal child.
It returns the exit status, both output streams, elapsed time, and which limits were
positively enforced, so a coordinator can classify the result without guessing.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

from lib_dst.provenance import (
    WORKER_PYTHON_HASH_SEED,
    repository_root,
    worker_environment,
)


@dataclass(frozen=True)
class WorkerLimits:
    """Per-worker limits. ``None`` means not requested."""

    timeout_seconds: float | None = None
    memory_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "memory_mb": self.memory_mb}

    @property
    def memory_enforceable(self) -> bool:
        return memory_limit_supported()


def memory_limit_supported() -> bool:
    """Address-space limits are enforced only where ``resource`` provides them."""

    if sys.platform == "win32":
        return False
    try:
        import resource  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class WorkerCompletion:
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_ns: int
    timed_out: bool
    limits: WorkerLimits
    memory_enforced: bool

    def last_json_line(self) -> str | None:
        lines = [line for line in self.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else None

    def limit_evidence(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.limits.timeout_seconds,
            "timed_out": self.timed_out,
            "memory_limit_mb": self.limits.memory_mb,
            "memory_limit": (
                "not-requested"
                if self.limits.memory_mb is None
                else "enforced" if self.memory_enforced else "unenforced"
            ),
        }


def _memory_preexec(memory_mb: int):
    def apply() -> None:  # pragma: no cover - runs in the forked child
        import resource

        limit = int(memory_mb) * 1024 * 1024
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        # Lower only the soft limit; raising or lowering the hard limit could fail or
        # needlessly constrain the child.
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))

    return apply


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_worker(
    arguments: Sequence[str],
    *,
    stdin: str | None = None,
    limits: WorkerLimits = WorkerLimits(),
    python_hash_seed: str = WORKER_PYTHON_HASH_SEED,
    revision: str | None = None,
) -> WorkerCompletion:
    """Run ``python -m lib_dst.worker <arguments>`` in a clean, pinned process."""

    memory_enforced = limits.memory_mb is not None and memory_limit_supported()
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "lib_dst.worker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=stdin,
            cwd=str(repository_root()),
            env=worker_environment(python_hash_seed=python_hash_seed, revision=revision),
            timeout=limits.timeout_seconds,
            preexec_fn=_memory_preexec(limits.memory_mb) if memory_enforced else None,
        )
    except subprocess.TimeoutExpired as exc:
        return WorkerCompletion(
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            elapsed_ns=time.perf_counter_ns() - started,
            timed_out=True,
            limits=limits,
            memory_enforced=memory_enforced,
        )
    return WorkerCompletion(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_ns=time.perf_counter_ns() - started,
        timed_out=False,
        limits=limits,
        memory_enforced=memory_enforced,
    )
