"""``om-dst``: the public Phase 5 command-line interface.

Invoke as ``python -m lib_dst`` (or the ``om-dst`` / ``om-dst.ps1`` wrappers at the
repository root)::

    om-dst run --corpus tests/dst_corpus
    om-dst run --seeds 0:500 [--profile small]
    om-dst run --stress-default [--master-seed 4500]
    om-dst run --stress-axis AXIS --discover-limit [--ceiling N]
    om-dst run --stress-axis AXIS --value N
    om-dst replay TRACE.json [--strict-env]
    om-dst shrink TRACE.json

Exit codes:

    0  every result passed or matched its declared expectation
    1  a correctness/engine failure, or a declared expected failure now passes (xpass)
    2  usage error or invalid input (bad arguments, malformed trace, corrupt corpus,
       shrink of a trace that does not fail)
    3  inconclusive: harness error, worker exception, or abnormal exit with no
       attributable cause
    4  replay environment incompatible with the trace under ``--strict-env``

stdout carries exactly one canonical JSON summary line. Progress goes to stderr.
All execution happens in clean worker processes; this coordinator process only
generates traces, classifies evidence, and is the sole writer of run artifacts.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from lib_dst.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    TOOL_NAME,
    RunDirectory,
    RunDirectoryError,
    canonical_json,
    sha256_text,
)
from lib_dst.process import WorkerCompletion, WorkerLimits, run_worker
from lib_dst.provenance import (
    WORKER_PYTHON_HASH_SEED,
    WORKER_TIMEZONE,
    dependency_versions,
    environment_mismatches,
    om_core_revision,
    provenance_record,
    repository_root,
)
from lib_dst.schemas import EnvironmentDescriptor, TraceTranscript


EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INCONCLUSIVE = 3
EXIT_ENVIRONMENT = 4

DEFAULT_OUT = repository_root() / ".om" / "dst-artifacts"
DEFAULT_MASTER_SEED = 4500
DEFAULT_BATCH_SIZE = 25
CORPUS_SCHEMA_VERSION = 1
KNOWN_FAILURES_SCHEMA_VERSION = 1

# Result statuses. Only "fail"/"xpass" and "inconclusive" affect the exit code.
PASS, FAIL, XFAIL, XPASS, INCONCLUSIVE, BOUNDARY = (
    "pass",
    "fail",
    "xfail",
    "xpass",
    "inconclusive",
    "boundary",
)
_STATUS_ORDER = (PASS, XFAIL, BOUNDARY, FAIL, XPASS, INCONCLUSIVE)
# Engine-attributed kinds whose evidence is worth re-collecting in an evaluate worker.
_EVIDENCE_KINDS = {"correctness_mismatch", "controlled_engine_error"}


class UsageError(Exception):
    """Invalid arguments or input. Maps to exit code 2."""


class EnvironmentRejected(Exception):
    """``--strict-env`` replay refused before execution. Maps to exit code 4."""


# --------------------------------------------------------------------------- helpers


def _log(message: str) -> None:
    print(f"[{TOOL_NAME}] {message}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def pinned_generation_environment() -> Iterator[None]:
    """Generate traces under the exact environment their workers use, then restore.

    Trace generators record ``EnvironmentDescriptor.current()`` in every trace, and
    that descriptor is part of the trace hash. Pinning it makes a CLI-generated trace
    independent of the caller's shell. The previous values are restored so an
    in-process caller (for example pytest) is unaffected.
    """

    saved = {key: os.environ.get(key) for key in ("PYTHONHASHSEED", "TZ")}
    os.environ["PYTHONHASHSEED"] = WORKER_PYTHON_HASH_SEED
    os.environ["TZ"] = WORKER_TIMEZONE
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def expected_worker_environment() -> dict[str, Any]:
    with pinned_generation_environment():
        return EnvironmentDescriptor.current().to_dict()


def _matches(expectation: dict[str, Any], classification: dict[str, Any]) -> bool:
    return all(
        classification.get(key) == value
        for key, value in expectation.items()
        if key in {"verdict", "origin", "kind", "code"}
    )


def result_status(
    classification: dict[str, Any],
    expectation: dict[str, Any] | None = None,
    *,
    boundary_kinds: frozenset[str] = frozenset(),
) -> str:
    verdict = classification.get("verdict")
    if classification.get("kind") in boundary_kinds:
        return BOUNDARY
    if expectation is None or expectation.get("verdict") == "passed":
        if verdict == "passed":
            return PASS
        return FAIL if verdict == "failed" else INCONCLUSIVE
    if _matches(expectation, classification):
        return XFAIL
    if verdict == "passed":
        return XPASS
    return FAIL if verdict == "failed" else INCONCLUSIVE


def exit_code_for(statuses: Sequence[str]) -> int:
    if any(status in {FAIL, XPASS} for status in statuses):
        return EXIT_FAILURES
    if any(status == INCONCLUSIVE for status in statuses):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def _counts(statuses: Sequence[str]) -> dict[str, int]:
    return {status: sum(1 for item in statuses if item == status) for status in _STATUS_ORDER}


def _parse_lines(stdout: str) -> list[dict[str, Any]]:
    parsed = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def _abnormal_classification(completion: WorkerCompletion) -> dict[str, Any]:
    from lib_dst.stress import StressClassification, classify_exit_status, timeout_classification

    if completion.timed_out:
        return timeout_classification().to_dict()
    if completion.returncode not in (0, None):
        return classify_exit_status(int(completion.returncode)).to_dict()
    return StressClassification(
        "inconclusive", "harness", "harness_error", "missing-worker-result"
    ).to_dict()


def _raw_evidence(completion: WorkerCompletion) -> dict[str, Any]:
    return {
        "returncode": completion.returncode,
        "stderr_tail": completion.stderr[-2000:],
        "stdout_line_count": len(completion.stdout.splitlines()),
        **completion.limit_evidence(),
    }


def read_trace(path: str | Path) -> TraceTranscript:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsageError(f"Cannot read trace {path}: {exc}") from exc
    try:
        return TraceTranscript.from_json(text)
    except (ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"Malformed trace JSON in {path}: {type(exc).__name__}: {exc}") from exc


def _load_json_file(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise UsageError(f"Cannot read {what} {path}: {exc}") from exc
    except ValueError as exc:
        raise UsageError(f"Malformed {what} JSON in {path}: {exc}") from exc


def load_known_failures(path: str | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = _load_json_file(Path(path), "known-failures file")
    if not isinstance(value, dict) or value.get("schema_version") != KNOWN_FAILURES_SCHEMA_VERSION:
        raise UsageError(f"Unsupported known-failures schema in {path}")
    axes = value.get("stress_axes", {})
    if not isinstance(axes, dict) or not all(isinstance(item, dict) for item in axes.values()):
        raise UsageError(f"Invalid stress_axes in {path}")
    return {str(axis): dict(expect) for axis, expect in axes.items()}


@dataclass(frozen=True)
class CorpusEntry:
    entry_id: str
    trace: TraceTranscript
    trace_json: str
    expect: dict[str, Any]
    relative_path: str


def load_corpus(root: str | Path) -> tuple[CorpusEntry, ...]:
    """Load and integrity-check a fixed regression corpus."""

    base = Path(root)
    manifest = _load_json_file(base / "manifest.json", "corpus manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise UsageError(f"Unsupported corpus manifest schema in {base}")
    entries = []
    seen: set[str] = set()
    for raw in manifest.get("entries", ()):
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("id"), str)
            or not isinstance(raw.get("trace"), str)
            or not isinstance(raw.get("expect", {}), dict)
        ):
            raise UsageError(f"Corpus manifest entry needs string id/trace and object expect: {raw!r}")
        entry_id = str(raw["id"])
        if entry_id in seen:
            raise UsageError(f"Duplicate corpus entry id: {entry_id}")
        seen.add(entry_id)
        relative = str(raw["trace"])
        trace = read_trace(base / relative)
        if trace.sha256() != raw.get("trace_sha256"):
            raise UsageError(
                f"Corpus entry {entry_id}: trace hash {trace.sha256()} does not match "
                f"manifest {raw.get('trace_sha256')}"
            )
        entries.append(
            CorpusEntry(entry_id, trace, trace.to_json(), dict(raw.get("expect", {})), relative)
        )
    if not entries:
        raise UsageError(f"Corpus {base} has no entries")
    return tuple(entries)


def _parse_seed_range(value: str) -> tuple[int, int]:
    try:
        start_text, count_text = value.split(":", 1)
        start, count = int(start_text), int(count_text)
    except ValueError as exc:
        raise UsageError(f"--seeds expects START:COUNT, got {value!r}") from exc
    if start < 0 or count < 1:
        raise UsageError("--seeds needs START >= 0 and COUNT >= 1")
    return start, count


def _master_seed(value: str) -> int | str:
    try:
        return int(value, 0)
    except ValueError:
        return value


# ------------------------------------------------------------------------ run state


@dataclass
class Session:
    run: RunDirectory
    limits: WorkerLimits
    shrink: bool
    revision: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    timings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def worker(self, arguments: Sequence[str], stdin: str | None = None, *, scale: int = 1) -> WorkerCompletion:
        limits = self.limits
        if scale > 1 and limits.timeout_seconds is not None:
            limits = WorkerLimits(limits.timeout_seconds * scale, limits.memory_mb)
        return run_worker(arguments, stdin=stdin, limits=limits, revision=self.revision)

    def evaluate(self, trace_json: str) -> tuple[dict[str, Any] | None, WorkerCompletion]:
        completion = self.worker(["--evaluate-stdin"], trace_json)
        lines = _parse_lines(completion.stdout)
        if completion.returncode == 0 and not completion.timed_out and lines:
            return lines[-1], completion
        return None, completion

    def environment_record(self, trace: TraceTranscript, worker_environment: dict[str, Any] | None) -> dict[str, Any]:
        recorded = trace.environment.to_dict()
        observed = worker_environment or expected_worker_environment()
        return {
            "recorded": recorded,
            "worker": observed,
            "mismatches": environment_mismatches(recorded, observed),
            "om_core_revision": self.revision,
            "dependencies": dependency_versions(),
        }

    def record_failure(
        self,
        trace: TraceTranscript,
        *,
        classification: dict[str, Any],
        evaluation: dict[str, Any] | None,
        raw_exit_evidence: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Write the original trace and evidence; shrink when a signature exists."""

        trace_json = trace.to_json()
        outcome = {
            "trace_sha256": trace.sha256(),
            "classification": classification,
            "deterministic_evidence": (evaluation or {}).get("deterministic_evidence"),
            "outcome": (evaluation or {}).get("outcome"),
            "scheduled": (evaluation or {}).get("scheduled"),
            "raw_exit_evidence": raw_exit_evidence or {},
        }
        if extra:
            outcome["context"] = extra
        diffs = (evaluation or {}).get("diffs") or []
        relative = self.run.write_failure(
            trace_json=trace_json,
            outcome=outcome,
            environment=self.environment_record(
                trace, (evaluation or {}).get("worker_environment")
            ),
            diff={"diffs": diffs} if diffs else None,
        )
        signature = ((evaluation or {}).get("deterministic_evidence") or {}).get(
            "failure_signature"
        )
        if self.shrink and signature is not None:
            self.shrink_into(relative, trace_json, signature)
        return relative

    def shrink_into(self, relative: str, trace_json: str, signature: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter_ns()
        completion = self.worker(["--shrink-stdin"], trace_json)
        lines = _parse_lines(completion.stdout)
        if completion.returncode != 0 or completion.timed_out or not lines:
            record = {
                "status": "shrink-worker-abnormal",
                "classification": _abnormal_classification(completion),
                "raw_exit_evidence": _raw_evidence(completion),
            }
        else:
            result = lines[-1]
            record = {key: value for key, value in result.items() if key != "minimized_json"}
            minimized_json = result.get("minimized_json")
            if result.get("status") == "minimized" and minimized_json:
                self.run.write_text(f"{relative}/minimized.json", minimized_json)
                replay, replay_completion = self.evaluate(minimized_json)
                replay_signature = (
                    (replay or {}).get("deterministic_evidence") or {}
                ).get("failure_signature")
                record["expected_signature"] = signature
                record["reproduced_in_clean_worker"] = replay_signature == signature
                if replay is None:
                    record["replay_raw_exit_evidence"] = _raw_evidence(replay_completion)
        self.run.write_json(f"{relative}/shrink.json", record)
        self.timings.append(
            {"unit": f"shrink:{relative}", "elapsed_ns": time.perf_counter_ns() - started}
        )
        return record

    def add_row(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        label = row.get("key")
        _log(f"{label}: {row['status']} ({row['classification'].get('kind')})")


def _open_session(args: argparse.Namespace, mode: str, command: dict[str, Any]) -> Session:
    limits = WorkerLimits(args.timeout, args.memory_mb)
    full_command = {
        **command,
        "limits": limits.to_dict(),
        "shrink": not args.no_shrink,
    }
    provenance = provenance_record()
    run = RunDirectory.open(
        args.out,
        mode=mode,
        command=full_command,
        provenance=provenance,
        overwrite=args.overwrite,
        resume=getattr(args, "resume", False),
    )
    if limits.memory_mb is not None and not limits.memory_enforceable:
        _log("warning: --memory-mb is not enforced on this platform; recorded as unenforced")
    session = Session(run, limits, not args.no_shrink, provenance["om_core_revision"])
    if limits.memory_mb is not None and not limits.memory_enforceable:
        session.warnings.append("memory-limit-unenforced")
    return session


def _finish(session: Session, mode: str, extras: dict[str, Any] | None = None, *, started_ns: int) -> tuple[int, dict[str, Any]]:
    statuses = [row["status"] for row in session.rows]
    exit_code = exit_code_for(statuses)
    summary = {
        "tool": TOOL_NAME,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "mode": mode,
        "command": session.run.manifest["command"],
        "results": session.rows,
        "counts": _counts(statuses),
        "exit_code": exit_code,
        **(extras or {}),
    }
    summary["identity_sha256"] = sha256_text(canonical_json(summary))
    elapsed = time.perf_counter_ns() - started_ns
    measurements = {
        "elapsed_ns_total": elapsed,
        "units": session.timings,
        "unit_count": len(session.rows),
        "units_per_second": (len(session.rows) / (elapsed / 1e9)) if elapsed else None,
        "note": "volatile; never part of any identity",
    }
    session.run.finalize(summary=summary, measurements=measurements, exit_code=exit_code)
    return exit_code, {
        "tool": TOOL_NAME,
        "mode": mode,
        "run_id": session.run.run_id,
        "artifact_dir": str(session.run.path),
        "exit_code": exit_code,
        "counts": summary["counts"],
        "identity_sha256": summary["identity_sha256"],
        "warnings": session.warnings,
    }


# ------------------------------------------------------------------------ run modes


def _row(key: str, trace_sha256: str | None, classification: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    row = {
        "key": key,
        "trace_sha256": trace_sha256,
        "classification": classification,
        "status": status,
    }
    row.update({name: value for name, value in extra.items() if value is not None})
    return row


def run_corpus(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started = time.perf_counter_ns()
    entries = load_corpus(args.corpus)
    command = {
        "corpus_entries": [[entry.entry_id, entry.trace.sha256()] for entry in entries],
    }
    session = _open_session(args, "corpus", command)
    for entry in entries:
        key = f"corpus:{entry.entry_id}"
        cached = session.run.load_checkpoint(key)
        if cached is not None:
            session.add_row(cached)
            continue
        unit_started = time.perf_counter_ns()
        evaluation, completion = session.evaluate(entry.trace_json)
        if evaluation is None:
            classification = _abnormal_classification(completion)
            raw = _raw_evidence(completion)
        else:
            classification = evaluation["classification"]
            raw = None
        status = result_status(classification, entry.expect or None)
        artifact = None
        if status in {FAIL, INCONCLUSIVE}:
            artifact = session.record_failure(
                entry.trace,
                classification=classification,
                evaluation=evaluation,
                raw_exit_evidence=raw,
                extra={"corpus_entry": entry.entry_id, "expect": entry.expect},
            )
        row = _row(
            key,
            entry.trace.sha256(),
            classification,
            status,
            expect=entry.expect or None,
            failure_artifact=artifact,
        )
        session.timings.append({"unit": key, "elapsed_ns": time.perf_counter_ns() - unit_started})
        session.run.checkpoint(key, row)
        session.add_row(row)
    return _finish(session, "corpus", started_ns=started)


def run_seeds(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    from lib_dst.evaluation import generate_seed_trace

    started = time.perf_counter_ns()
    start, count = _parse_seed_range(args.seeds)
    if args.batch_size < 1:
        raise UsageError("--batch-size must be positive")
    if args.profile is not None:
        from lib_dst.profiles import profile_names

        if args.profile not in profile_names():
            raise UsageError(f"Unknown profile {args.profile!r}; choose from {profile_names()}")
    command = {
        "seeds": [start, count],
        "profile": args.profile,
        "batch_size": args.batch_size,
        "generator": "legacy" if args.profile is None else "profile",
    }
    session = _open_session(args, "seeds", command)
    profile_args = ["--profile", args.profile] if args.profile else []

    def seed_key(seed: int) -> str:
        return f"seed:{seed}"

    def handle_line(line: dict[str, Any]) -> dict[str, Any]:
        seed = int(line["seed"])
        classification = line["classification"]
        status = result_status(classification)
        artifact = None
        if status != PASS and line.get("trace_json"):
            trace = TraceTranscript.from_json(line["trace_json"])
            artifact = session.record_failure(
                trace,
                classification=classification,
                evaluation=line,
                extra={"seed": seed, "profile": args.profile},
            )
        return _row(seed_key(seed), line.get("trace_sha256"), classification, status, failure_artifact=artifact)

    for batch_start in range(start, start + count, args.batch_size):
        batch_count = min(args.batch_size, start + count - batch_start)
        batch_key = f"seeds-{batch_start}-{batch_count}"
        cached = session.run.load_checkpoint(batch_key)
        if cached is not None:
            for row in cached:
                session.add_row(row)
            continue
        unit_started = time.perf_counter_ns()
        completion = session.worker(
            ["--seed-outcomes", "--start-seed", str(batch_start), "--count", str(batch_count), *profile_args],
            scale=batch_count,
        )
        lines = {int(line["seed"]): line for line in _parse_lines(completion.stdout) if "seed" in line}
        rows: list[dict[str, Any]] = []
        for seed in range(batch_start, batch_start + batch_count):
            line = lines.get(seed)
            if line is None:
                # The batch ended abnormally before this seed reported. Re-run it alone
                # so a crash is attributed to exactly one seed with its own evidence.
                single = session.worker(
                    ["--seed-outcomes", "--start-seed", str(seed), "--count", "1", *profile_args]
                )
                single_lines = [item for item in _parse_lines(single.stdout) if item.get("seed") == seed]
                if single_lines and single.returncode == 0 and not single.timed_out:
                    line = single_lines[-1]
                else:
                    classification = _abnormal_classification(single)
                    with pinned_generation_environment():
                        trace = generate_seed_trace(seed, args.profile)
                    artifact = session.record_failure(
                        trace,
                        classification=classification,
                        evaluation=None,
                        raw_exit_evidence=_raw_evidence(single),
                        extra={"seed": seed, "profile": args.profile},
                    )
                    row = _row(
                        seed_key(seed),
                        trace.sha256(),
                        classification,
                        result_status(classification),
                        failure_artifact=artifact,
                    )
                    rows.append(row)
                    session.add_row(row)
                    continue
            row = handle_line(line)
            rows.append(row)
            session.add_row(row)
        session.timings.append(
            {"unit": batch_key, "elapsed_ns": time.perf_counter_ns() - unit_started, "seeds": batch_count}
        )
        session.run.checkpoint(batch_key, rows)
    return _finish(session, "seeds", started_ns=started)


def _stress_point(session: Session, trace: TraceTranscript, axis: str, value: int):
    from lib_dst.stress import StressPointResult, run_stress_point_in_worker

    key = f"stress:{axis}:{value}"
    cached = session.run.load_checkpoint(key)
    if cached is not None:
        return StressPointResult.from_dict(cached)
    point = run_stress_point_in_worker(
        trace,
        axis=axis,
        requested_value=value,
        python_hash_seed=WORKER_PYTHON_HASH_SEED,
        timeout_seconds=session.limits.timeout_seconds,
        memory_mb=session.limits.memory_mb,
    )
    session.timings.append({"unit": key, "elapsed_ns": point.measurements.elapsed_ns})
    session.run.checkpoint(key, point.to_dict())
    return point


def _stress_rows(
    session: Session,
    points,
    traces: dict[tuple[str, int], TraceTranscript],
    known: dict[str, dict[str, Any]],
    *,
    boundary_kinds: frozenset[str] = frozenset(),
) -> None:
    for point in points:
        classification = point.classification.to_dict()
        expectation = known.get(point.axis)
        status = result_status(classification, expectation, boundary_kinds=boundary_kinds)
        artifact = None
        trace = traces.get((point.axis, point.requested_value))
        if status in {FAIL, INCONCLUSIVE} and trace is not None:
            evaluation = None
            if point.classification.kind in _EVIDENCE_KINDS:
                evaluation, _ = session.evaluate(trace.to_json())
            artifact = session.record_failure(
                trace,
                classification=classification,
                evaluation=evaluation,
                raw_exit_evidence=point.raw_exit_evidence,
                extra={"axis": point.axis, "requested_value": point.requested_value},
            )
        session.add_row(
            _row(
                f"stress:{point.axis}:{point.requested_value}",
                point.trace_sha256,
                classification,
                status,
                expect=expectation,
                failure_artifact=artifact,
            )
        )


def run_stress(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    from lib_dst.scale_generator import generate_stress_axis_trace
    from lib_dst.stress import STRESS_AXES, STRESS_DEFAULT_VALUES, StressCampaignReport, run_limit_discovery

    started = time.perf_counter_ns()
    master_seed = _master_seed(args.master_seed)
    known = load_known_failures(args.known_failures)
    traces: dict[tuple[str, int], TraceTranscript] = {}

    if args.stress_default:
        mode, preset = "stress_default", "stress_default"
        values: tuple[tuple[str, int], ...] = STRESS_DEFAULT_VALUES
    else:
        axis = args.stress_axis
        if axis not in STRESS_AXES:
            raise UsageError(f"Unknown stress axis {axis!r}; choose from {sorted(STRESS_AXES)}")
        if args.discover_limit == (args.value is not None):
            raise UsageError("--stress-axis needs exactly one of --discover-limit or --value N")
        if args.value is not None and args.value < 1:
            raise UsageError("--value must be positive")
        mode = "discover" if args.discover_limit else "stress_point"
        preset = f"discover:{axis}" if args.discover_limit else f"point:{axis}"
        values = () if args.discover_limit else ((axis, args.value),)

    command = {
        "preset": preset,
        "master_seed": master_seed,
        "ceiling": args.ceiling,
        "known_failures": known,
    }
    if values:
        command["values"] = [list(item) for item in values]
    session = _open_session(args, mode, command)

    def execute(trace: TraceTranscript, axis_name: str, value: int):
        traces[(axis_name, value)] = trace
        point = _stress_point(session, trace, axis_name, value)
        _log(f"stress {axis_name}={value}: {point.classification.kind}")
        return point

    with pinned_generation_environment():
        if values:
            points = []
            for axis_name, value in values:
                points.append(execute(generate_stress_axis_trace(master_seed, axis_name, value), axis_name, value))
            report = StressCampaignReport(preset, master_seed, tuple(points))
            boundary_kinds: frozenset[str] = frozenset()
        else:
            report = run_limit_discovery(
                master_seed, args.stress_axis, ceiling=args.ceiling, executor=execute
            )
            boundary_kinds = frozenset({"safety_ceiling", "generation_rejected"})

    report_name = "stress_default" if args.stress_default else args.stress_axis
    session.run.write_json(f"stress/{report_name}.json", report.to_dict())
    _stress_rows(session, report.points, traces, known, boundary_kinds=boundary_kinds)
    extras = {
        "stress_report": f"stress/{report_name}.json",
        "stress_report_identity_sha256": report.sha256(),
    }
    if not values:
        extras.update(
            last_observed_pass=report.last_observed_pass,
            first_observed_failure=report.first_observed_failure,
            monotonic=report.monotonic,
        )
    return _finish(session, mode, extras, started_ns=started)


def run_replay(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started = time.perf_counter_ns()
    trace = read_trace(args.trace)
    mismatches = environment_mismatches(trace.environment.to_dict(), expected_worker_environment())
    if args.strict_env and mismatches:
        raise EnvironmentRejected(json.dumps({"mismatches": mismatches}, sort_keys=True))
    command = {"trace_sha256": trace.sha256(), "strict_env": args.strict_env}
    session = _open_session(args, "replay", command)
    if mismatches:
        session.warnings.append("environment-mismatch")
        _log(
            "warning: replay environment differs from the trace's recorded environment: "
            + ", ".join(item["field"] for item in mismatches)
        )
    evaluation, completion = session.evaluate(trace.to_json())
    if evaluation is None:
        classification = _abnormal_classification(completion)
        raw = _raw_evidence(completion)
    else:
        classification = evaluation["classification"]
        raw = None
    status = result_status(classification)
    artifact = None
    if status != PASS:
        artifact = session.record_failure(
            trace, classification=classification, evaluation=evaluation, raw_exit_evidence=raw
        )
    session.add_row(_row("replay", trace.sha256(), classification, status, failure_artifact=artifact))
    return _finish(
        session,
        "replay",
        {"environment_mismatches": mismatches},
        started_ns=started,
    )


def run_shrink(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started = time.perf_counter_ns()
    trace = read_trace(args.trace)
    args.no_shrink = False
    session = _open_session(args, "shrink", {"trace_sha256": trace.sha256()})
    evaluation, completion = session.evaluate(trace.to_json())
    if evaluation is None:
        classification = _abnormal_classification(completion)
        session.record_failure(
            trace,
            classification=classification,
            evaluation=None,
            raw_exit_evidence=_raw_evidence(completion),
        )
        session.add_row(_row("shrink", trace.sha256(), classification, INCONCLUSIVE))
        return _finish(session, "shrink", started_ns=started)
    signature = (evaluation.get("deterministic_evidence") or {}).get("failure_signature")
    classification = evaluation["classification"]
    if signature is None:
        if classification.get("verdict") == "passed":
            raise UsageError("Trace passes; there is no failure to shrink")
        raise UsageError(
            "Trace failure has no normalized signature "
            f"({classification.get('kind')}/{classification.get('code')}); "
            "only signature-identified failures can be shrunk"
        )
    session.shrink = False  # shrink explicitly below, once
    relative = session.record_failure(trace, classification=classification, evaluation=evaluation)
    record = session.shrink_into(relative, trace.to_json(), signature)
    if record.get("status") == "minimized" and record.get("reproduced_in_clean_worker"):
        status = PASS
    else:
        status = INCONCLUSIVE
    session.add_row(
        _row(
            "shrink",
            trace.sha256(),
            classification,
            status,
            failure_artifact=relative,
            minimized_sha256=record.get("minimized_sha256"),
            signature=signature,
        )
    )
    return _finish(session, "shrink", started_ns=started)


# ---------------------------------------------------------------------------- parser


def _common(parser: argparse.ArgumentParser, *, resume: bool) -> None:
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="artifact root directory")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing run directory")
    if resume:
        parser.add_argument("--resume", action="store_true", help="continue an incomplete run")
    parser.add_argument("--timeout", type=float, default=None, help="per-trace worker wall-clock limit in seconds")
    parser.add_argument("--memory-mb", type=int, default=None, help="worker address-space limit (POSIX only)")
    parser.add_argument("--no-shrink", action="store_true", help="do not minimize failures")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Deterministic simulation testing for OM Core (local serial engine).",
        epilog="Exit codes: 0 ok, 1 failures, 2 usage/invalid input, 3 inconclusive, "
        "4 incompatible replay environment.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a corpus, seed batch, or stress campaign")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--corpus", help="fixed regression corpus directory (manifest.json)")
    target.add_argument("--seeds", help="seed range START:COUNT")
    target.add_argument("--stress-default", action="store_true", help="bounded stress_default preset")
    target.add_argument("--stress-axis", help="single stress axis")
    run.add_argument("--profile", help="workload profile for --seeds (default: legacy generator)")
    run.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="seeds per worker process")
    run.add_argument("--discover-limit", action="store_true", help="limit discovery for --stress-axis")
    run.add_argument("--value", type=int, help="single requested value for --stress-axis")
    run.add_argument("--ceiling", type=int, help="limit-discovery safety ceiling")
    run.add_argument("--master-seed", default=str(DEFAULT_MASTER_SEED), help="stress master seed")
    run.add_argument("--known-failures", help="known stress-axis failures JSON")
    _common(run, resume=True)

    replay = commands.add_parser("replay", help="replay one standalone trace in a clean worker")
    replay.add_argument("trace", help="trace JSON file")
    replay.add_argument("--strict-env", action="store_true", help="refuse an incompatible environment (exit 4)")
    _common(replay, resume=False)

    shrink = commands.add_parser("shrink", help="minimize one failing trace in a clean worker")
    shrink.add_argument("trace", help="trace JSON file")
    _common(shrink, resume=False)
    return parser


def _validate_run_arguments(args: argparse.Namespace) -> None:
    if args.profile is not None and args.seeds is None:
        raise UsageError("--profile applies only to --seeds")
    if (args.discover_limit or args.value is not None or args.ceiling is not None) and args.stress_axis is None:
        raise UsageError("--discover-limit, --value, and --ceiling apply only to --stress-axis")
    if args.ceiling is not None and not args.discover_limit:
        raise UsageError("--ceiling applies only to --discover-limit")
    if args.known_failures is not None and not (args.stress_default or args.stress_axis):
        raise UsageError("--known-failures applies only to stress modes")
    if args.timeout is not None and args.timeout <= 0:
        raise UsageError("--timeout must be positive")
    if args.memory_mb is not None and args.memory_mb < 1:
        raise UsageError("--memory-mb must be positive")


def dispatch(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.command == "run":
        _validate_run_arguments(args)
        if args.corpus is not None:
            return run_corpus(args)
        if args.seeds is not None:
            return run_seeds(args)
        return run_stress(args)
    if args.timeout is not None and args.timeout <= 0:
        raise UsageError("--timeout must be positive")
    if args.command == "replay":
        return run_replay(args)
    return run_shrink(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # argparse exits 2 on usage errors
    try:
        exit_code, summary = dispatch(args)
    except UsageError as exc:
        _log(f"error: {exc}")
        exit_code, summary = EXIT_USAGE, {"tool": TOOL_NAME, "exit_code": EXIT_USAGE, "error": str(exc)}
    except RunDirectoryError as exc:
        _log(f"error: {exc}")
        exit_code, summary = EXIT_USAGE, {"tool": TOOL_NAME, "exit_code": EXIT_USAGE, "error": str(exc)}
    except EnvironmentRejected as exc:
        _log(f"error: incompatible replay environment: {exc}")
        exit_code = EXIT_ENVIRONMENT
        summary = {
            "tool": TOOL_NAME,
            "exit_code": EXIT_ENVIRONMENT,
            "error": "incompatible-environment",
            "detail": json.loads(str(exc)),
        }
    except Exception as exc:  # noqa: BLE001
        # A coordinator defect must not masquerade as an engine failure (exit 1).
        import traceback

        traceback.print_exc(file=sys.stderr)
        exit_code = EXIT_INCONCLUSIVE
        summary = {
            "tool": TOOL_NAME,
            "exit_code": EXIT_INCONCLUSIVE,
            "error": "coordinator-exception",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    print(canonical_json(summary), flush=True)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
