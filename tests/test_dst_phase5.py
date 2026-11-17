"""Phase 5: public om-dst CLI, artifacts, regression corpus, and worker limits."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib_dst import cli
from lib_dst.artifacts import RunDirectory, RunDirectoryError, atomic_write_text, safe_key
from lib_dst.diff import canonical_diff
from lib_dst.failures import capture_failure
from lib_dst.faults import FaultDecision, with_faults
from lib_dst.generator import generate_trace
from lib_dst.process import WorkerLimits, run_worker
from lib_dst.provenance import environment_mismatches, repository_root, worker_environment
from lib_dst.runner import run_failure_in_worker
from lib_dst.scale_generator import generate_stress_axis_trace
from lib_dst.stress import (
    StressClassification,
    StressMeasurements,
    StressPointResult,
    classify_exit_status,
    classify_worker_result,
    run_stress_point_in_worker,
)

ROOT = repository_root()
CORPUS = ROOT / "tests" / "dst_corpus"
KNOWN_FAILURES = CORPUS / "known_failures.json"


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out  # exactly one JSON summary line on stdout
    return code, json.loads(out[0])


def _write_trace(path: Path, trace) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trace.to_json(), encoding="utf-8")
    return path


def _fault_trace():
    with cli.pinned_generation_environment():
        return with_faults(generate_trace(3), [FaultDecision("persistence", "save", 1, "raise")])


# ---------------------------------------------------------------- classification


def test_exit_status_uses_unsigned_ntstatus_and_posix_signals() -> None:
    assert classify_exit_status(3221225495).kind == "resource_exhaustion"  # 0xC0000017
    assert classify_exit_status(3221225725).kind == "resource_exhaustion"  # 0xC00000FD
    assert classify_exit_status(3221225477).kind == "worker_crash"  # 0xC0000005
    assert classify_exit_status(3221226505).kind == "worker_crash"  # 0xC0000409
    # Signed spellings from the Phase 4.5 contract remain recognized.
    assert classify_exit_status(-1073741801).kind == "resource_exhaustion"
    assert classify_exit_status(-1073741819).kind == "worker_crash"
    assert classify_exit_status(-11).code == "signal:SIGSEGV"
    assert classify_exit_status(-11).kind == "worker_crash"
    assert classify_exit_status(-24).kind == "resource_exhaustion"
    # SIGKILL can be the OOM killer or an external kill: never inferred.
    assert classify_exit_status(-9).kind == "unknown_abnormal_exit"
    assert classify_exit_status(17).kind == "unknown_abnormal_exit"


def test_real_abnormal_child_exit_is_classified_from_platform_evidence() -> None:
    if sys.platform == "win32":
        code = "import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000017)"
        expected = "resource_exhaustion"
    else:
        code = "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"
        expected = "worker_crash"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    classification, _, _, raw = classify_worker_result(
        returncode=completed.returncode, stdout="", stderr="", elapsed_ns=1
    )
    assert completed.returncode != 0
    assert classification.kind == expected
    assert raw["returncode"] == completed.returncode


def test_timeout_is_a_positively_enforced_host_dependent_limit() -> None:
    trace = generate_trace(0)
    point = run_stress_point_in_worker(trace, axis="probe", requested_value=1, timeout_seconds=0.01)
    assert point.classification.kind == "resource_exhaustion"
    assert point.classification.origin == "policy"
    assert point.classification.code == "wall-timeout"
    assert point.raw_exit_evidence["timed_out"] is True
    assert point.raw_exit_evidence["timeout_seconds"] == 0.01


def test_memory_limit_evidence_is_explicit_about_enforcement() -> None:
    completion = run_worker(
        ["--evaluate-stdin"],
        stdin=generate_trace(0).to_json(),
        limits=WorkerLimits(timeout_seconds=120, memory_mb=8192),
    )
    evidence = completion.limit_evidence()
    assert completion.returncode == 0
    assert evidence["memory_limit"] == ("unenforced" if sys.platform == "win32" else "enforced")


def test_stress_identity_excludes_revision_but_reports_it() -> None:
    def point(revision: str) -> StressPointResult:
        return StressPointResult(
            "axis", 1, 1, "sha", StressClassification("passed", "none", "pass", "ok"),
            {}, StressMeasurements(1, 1, 1, 1), {"python": "x"}, revision, {},
        )

    first, second = point("rev-a"), point("rev-b")
    assert first.identity_dict() == second.identity_dict()
    assert "om_core_revision" not in first.identity_dict()
    assert first.to_dict()["om_core_revision"] == "rev-a"
    assert StressPointResult.from_dict(first.to_dict()).om_core_revision == "rev-a"


# ---------------------------------------------------------- provenance and diffs


def test_environment_mismatches_name_each_differing_field() -> None:
    recorded = {"python": "3.12.1", "platform": "win32", "timezone": "UTC"}
    observed = {"python": "3.12.1", "platform": "linux", "timezone": "system"}
    fields = [item["field"] for item in environment_mismatches(recorded, observed)]
    assert "platform" in fields and "timezone" in fields and "python" not in fields


def test_worker_environment_pins_hash_seed_timezone_and_import_path() -> None:
    environment = worker_environment(revision="r", base={"PYTHONPATH": "elsewhere"})
    assert environment["PYTHONHASHSEED"] == "1"
    assert environment["TZ"] == "UTC"
    assert environment["OM_CORE_REVISION"] == "r"
    assert environment["PYTHONPATH"].split(os.pathsep) == [str(ROOT), "elsewhere"]


def test_pinned_generation_environment_restores_caller_state(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONHASHSEED", "999")
    monkeypatch.delenv("TZ", raising=False)
    with cli.pinned_generation_environment():
        assert generate_trace(0).environment.python_hash_seed == "1"
        assert generate_trace(0).environment.timezone == "UTC"
    assert os.environ["PYTHONHASHSEED"] == "999"
    assert "TZ" not in os.environ


def test_canonical_diff_is_stable_type_aware_and_truncated() -> None:
    expected = {"a": 1, "b": [1, 2], "c": True}
    actual = {"a": 2, "b": [1], "c": 1, "d": None}
    first = canonical_diff(expected, actual)
    assert first == canonical_diff(expected, actual)
    assert [item["path"] for item in first["differences"]] == ["$.a", "$.b[1]", "$.c", "$.d"]
    assert canonical_diff(expected, expected)["equal"] is True
    truncated = canonical_diff(list(range(10)), list(range(10, 20)), limit=3)
    assert truncated["truncated"] and truncated["difference_count"] == 10
    assert len(truncated["differences"]) == 3


def test_convergence_evidence_exposes_every_checked_observation() -> None:
    evidence: dict = {}
    outcome = capture_failure(generate_trace(0), evidence=evidence)
    assert not outcome.failed
    assert set(evidence) == {
        "incremental",
        "cache-first-recalculation",
        "cache-second-recalculation",
        "snapshot-roundtrip",
        "fresh-engine-convergence",
    }
    assert all(value == evidence["incremental"] for value in evidence.values())


def test_legacy_runner_works_from_a_foreign_working_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_failure_in_worker(generate_trace(1))
    assert result["failed"] is False


# --------------------------------------------------------------------- artifacts


def test_run_directory_collision_resume_and_overwrite_rules(tmp_path) -> None:
    command = {"x": 1}
    run = RunDirectory.open(tmp_path, mode="m", command=command, provenance={})
    assert json.loads((run.path / "manifest.json").read_text())["status"] == "incomplete"
    with pytest.raises(RunDirectoryError, match="incomplete"):
        RunDirectory.open(tmp_path, mode="m", command=command, provenance={})
    resumed = RunDirectory.open(tmp_path, mode="m", command=command, provenance={}, resume=True)
    assert resumed.resumed
    resumed.finalize(summary={}, measurements={}, exit_code=0)
    with pytest.raises(RunDirectoryError, match="already complete"):
        RunDirectory.open(tmp_path, mode="m", command=command, provenance={}, resume=True)
    replaced = RunDirectory.open(tmp_path, mode="m", command=command, provenance={}, overwrite=True)
    assert not (replaced.path / "summary.json").exists()

    foreign = tmp_path / RunDirectory.open(tmp_path, mode="n", command={}, provenance={}).run_id
    (foreign / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RunDirectoryError, match="not an om-dst run directory"):
        RunDirectory.open(tmp_path, mode="n", command={}, provenance={}, overwrite=True)
    assert foreign.exists()  # a directory we do not own is never deleted


def test_failure_artifact_retains_byte_exact_standalone_trace(tmp_path) -> None:
    run = RunDirectory.open(tmp_path / "with space", mode="m", command={}, provenance={})
    trace = generate_trace(2)
    relative = run.write_failure(trace_json=trace.to_json(), outcome={}, environment={})
    written = (run.path / relative / "trace.json").read_bytes()
    assert hashlib.sha256(written).hexdigest() == trace.sha256()
    assert relative.endswith(trace.sha256())


def test_atomic_write_and_safe_keys(tmp_path) -> None:
    target = tmp_path / "nested" / "value.json"
    atomic_write_text(target, "a")
    atomic_write_text(target, "b")
    assert target.read_text() == "b"
    assert not [path for path in target.parent.iterdir() if path.name.endswith(".tmp")]
    assert safe_key("seed:12") == "seed_12"
    with pytest.raises(ValueError):
        safe_key("..")


# -------------------------------------------------------------------------- CLI


def test_seed_run_is_repeatable_refuses_collision_and_overwrites(tmp_path, capsys) -> None:
    argv = ["run", "--seeds", "0:3", "--batch-size", "2", "--out", str(tmp_path)]
    code, summary = _run(capsys, argv)
    assert code == cli.EXIT_OK
    assert summary["counts"]["pass"] == 3
    run_dir = Path(summary["artifact_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete" and manifest["exit_code"] == 0
    assert manifest["provenance"]["worker_environment"] == {"PYTHONHASHSEED": "1", "TZ": "UTC"}

    code, again = _run(capsys, argv)
    assert code == cli.EXIT_USAGE and "already complete" in again["error"]

    code, replaced = _run(capsys, [*argv, "--overwrite"])
    assert code == cli.EXIT_OK
    assert replaced["identity_sha256"] == summary["identity_sha256"]


def test_interrupted_seed_run_resumes_to_identical_identity(tmp_path, capsys) -> None:
    argv = ["run", "--seeds", "0:2", "--batch-size", "1", "--out", str(tmp_path)]
    code, complete = _run(capsys, argv)
    assert code == 0
    run_dir = Path(complete["artifact_dir"])
    # Simulate a coordinator killed after the first batch.
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["status"] = "incomplete"
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "summary.json").unlink()
    (run_dir / "checkpoints" / "seeds-1-1.json").unlink()

    code, refused = _run(capsys, argv)
    assert code == cli.EXIT_USAGE and "incomplete" in refused["error"]
    code, resumed = _run(capsys, [*argv, "--resume"])
    assert code == cli.EXIT_OK
    assert resumed["identity_sha256"] == complete["identity_sha256"]
    assert json.loads((run_dir / "manifest.json").read_text())["resumed"] is True


def test_seed_batch_abnormal_exit_is_localized_to_one_seed(tmp_path, capsys) -> None:
    code, summary = _run(
        capsys, ["run", "--seeds", "4:1", "--timeout", "0.01", "--no-shrink", "--out", str(tmp_path)]
    )
    assert code == cli.EXIT_FAILURES
    row = json.loads((Path(summary["artifact_dir"]) / "summary.json").read_text())["results"][0]
    assert row["key"] == "seed:4"
    assert row["classification"]["code"] == "wall-timeout"
    with cli.pinned_generation_environment():
        expected_sha = generate_trace(4).sha256()
    assert row["trace_sha256"] == expected_sha
    trace_file = Path(summary["artifact_dir"]) / row["failure_artifact"] / "trace.json"
    assert hashlib.sha256(trace_file.read_bytes()).hexdigest() == expected_sha
    outcome = json.loads(trace_file.with_name("outcome.json").read_text())
    assert outcome["raw_exit_evidence"]["timed_out"] is True


def test_replay_failure_publishes_trace_diff_environment_and_minimized_trace(tmp_path, capsys) -> None:
    trace_path = _write_trace(tmp_path / "dir with spaces" / "fault trace.json", _fault_trace())
    code, summary = _run(capsys, ["replay", str(trace_path), "--out", str(tmp_path / "out")])
    assert code == cli.EXIT_FAILURES
    full = json.loads((Path(summary["artifact_dir"]) / "summary.json").read_text())
    failure = Path(summary["artifact_dir"]) / full["results"][0]["failure_artifact"]
    assert {path.name for path in failure.iterdir()} >= {
        "trace.json", "outcome.json", "environment.json", "minimized.json", "shrink.json"
    }
    assert hashlib.sha256((failure / "trace.json").read_bytes()).hexdigest() == failure.name
    shrink = json.loads((failure / "shrink.json").read_text())
    assert shrink["reproduced_in_clean_worker"] is True
    environment = json.loads((failure / "environment.json").read_text())
    assert environment["mismatches"] == []
    outcome = json.loads((failure / "outcome.json").read_text())
    assert outcome["classification"]["code"] == "persistence.save.injected"


def test_strict_replay_rejects_incompatible_environment_before_execution(tmp_path, capsys) -> None:
    trace = generate_trace(0)
    data = trace.to_dict()
    data["environment"]["platform"] = "not-this-platform"
    path = tmp_path / "foreign.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    code, summary = _run(capsys, ["replay", str(path), "--strict-env", "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_ENVIRONMENT
    assert summary["detail"]["mismatches"][0]["field"] == "platform"
    assert not (tmp_path / "o").exists()
    code, warned = _run(capsys, ["replay", str(path), "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_OK and warned["warnings"] == ["environment-mismatch"]


def test_malformed_input_and_bad_arguments_are_usage_errors(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1, "operations": [', encoding="utf-8")
    assert _run(capsys, ["replay", str(bad), "--out", str(tmp_path)])[0] == cli.EXIT_USAGE
    assert _run(capsys, ["replay", str(tmp_path / "missing.json")])[0] == cli.EXIT_USAGE
    assert _run(capsys, ["run", "--seeds", "nope", "--out", str(tmp_path)])[0] == cli.EXIT_USAGE
    assert _run(capsys, ["run", "--stress-axis", "cube_count", "--out", str(tmp_path)])[0] == cli.EXIT_USAGE
    assert _run(capsys, ["run", "--seeds", "0:1", "--profile", "nope", "--out", str(tmp_path)])[0] == cli.EXIT_USAGE
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run"])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_shrink_of_passing_trace_is_a_usage_error(tmp_path, capsys) -> None:
    path = _write_trace(tmp_path / "pass.json", generate_trace(0))
    code, summary = _run(capsys, ["shrink", str(path), "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_USAGE and "passes" in summary["error"]


def test_shrink_command_minimizes_and_verifies_in_clean_worker(tmp_path, capsys) -> None:
    trace = _fault_trace()
    path = _write_trace(tmp_path / "fault.json", trace)
    code, summary = _run(capsys, ["shrink", str(path), "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_OK
    row = json.loads((Path(summary["artifact_dir"]) / "summary.json").read_text())["results"][0]
    minimized = Path(summary["artifact_dir"]) / row["failure_artifact"] / "minimized.json"
    assert hashlib.sha256(minimized.read_bytes()).hexdigest() == row["minimized_sha256"]
    assert len(minimized.read_bytes()) < len(trace.to_json())


def _mini_corpus(root: Path, entries: list[tuple[str, object, dict]]) -> Path:
    manifest = {"schema_version": 1, "entries": []}
    for entry_id, trace, expect in entries:
        relative = f"traces/{trace.sha256()}.json"
        _write_trace(root / relative, trace)
        manifest["entries"].append(
            {"id": entry_id, "trace": relative, "trace_sha256": trace.sha256(), "expect": expect}
        )
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_corpus_expectations_produce_pass_xfail_fail_and_xpass(tmp_path, capsys) -> None:
    fault = _fault_trace()
    expected_fault = {"verdict": "failed", "kind": "controlled_engine_error", "code": "persistence.save.injected"}
    corpus = _mini_corpus(
        tmp_path / "corpus",
        [
            ("pass", generate_trace(0), {"verdict": "passed"}),
            ("xfail", fault, expected_fault),
            ("xpass", generate_trace(1), expected_fault),
        ],
    )
    code, summary = _run(capsys, ["run", "--corpus", str(corpus), "--no-shrink", "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_FAILURES
    assert summary["counts"]["pass"] == 1
    assert summary["counts"]["xfail"] == 1
    assert summary["counts"]["xpass"] == 1


def test_corpus_hash_mismatch_is_rejected(tmp_path, capsys) -> None:
    corpus = _mini_corpus(tmp_path / "c", [("one", generate_trace(0), {"verdict": "passed"})])
    trace_file = next((corpus / "traces").iterdir())
    data = json.loads(trace_file.read_text())
    data["metadata"]["tampered"] = True
    trace_file.write_text(json.dumps(data), encoding="utf-8")
    code, summary = _run(capsys, ["run", "--corpus", str(corpus), "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_USAGE and "does not match manifest" in summary["error"]


def test_malformed_corpus_and_known_failures_are_usage_errors(tmp_path, capsys) -> None:
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "entries": [{"trace": "x.json"}]}), encoding="utf-8"
    )
    code, summary = _run(capsys, ["run", "--corpus", str(corpus), "--out", str(tmp_path / "o")])
    assert code == cli.EXIT_USAGE and "string id/trace" in summary["error"]
    known = tmp_path / "known.json"
    known.write_text(json.dumps({"schema_version": 1, "stress_axes": {"udf_depth": 1}}), encoding="utf-8")
    code, _ = _run(
        capsys,
        ["run", "--stress-axis", "udf_depth", "--value", "1", "--known-failures", str(known), "--out", str(tmp_path)],
    )
    assert code == cli.EXIT_USAGE


def test_coordinator_exception_is_inconclusive_not_a_failure(tmp_path, capsys, monkeypatch) -> None:
    def broken(_args):
        raise KeyError("coordinator bug")

    monkeypatch.setattr(cli, "run_seeds", broken)
    code, summary = _run(capsys, ["run", "--seeds", "0:1", "--out", str(tmp_path)])
    assert code == cli.EXIT_INCONCLUSIVE
    assert summary["error"] == "coordinator-exception"


def test_checked_in_corpus_is_internally_consistent() -> None:
    entries = cli.load_corpus(CORPUS)
    ids = {entry.entry_id for entry in entries}
    assert {"known-defect-udf-create", "known-defect-format-address"} <= ids
    assert any(entry.trace.metadata.get("scheduler") for entry in entries)
    for entry in entries:
        assert entry.relative_path == f"traces/{entry.trace.sha256()}.json"
        assert (CORPUS / entry.relative_path).read_text(encoding="utf-8") == entry.trace_json


def test_corpus_builder_reproduces_checked_in_corpus_on_matching_environment() -> None:
    entry = next(item for item in cli.load_corpus(CORPUS) if item.entry_id == "legacy-seed-0")
    if environment_mismatches(entry.trace.environment.to_dict(), cli.expected_worker_environment()):
        pytest.skip("corpus was generated on a different Python/platform")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dst_build_corpus.py"), "--check"],
        capture_output=True, text=True, check=False, cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_known_stress_failures_are_xfail_and_unknown_ones_fail(tmp_path, capsys) -> None:
    base = ["run", "--stress-axis", "udf_depth", "--value", "1", "--no-shrink"]
    code, summary = _run(capsys, [*base, "--known-failures", str(KNOWN_FAILURES), "--out", str(tmp_path / "a")])
    assert code == cli.EXIT_OK and summary["counts"]["xfail"] == 1
    code, summary = _run(capsys, [*base, "--out", str(tmp_path / "b")])
    assert code == cli.EXIT_FAILURES and summary["counts"]["fail"] == 1
    run_dir = Path(summary["artifact_dir"])
    assert (run_dir / "stress" / "udf_depth.json").is_file()
    assert list((run_dir / "failures").iterdir())


def test_limit_discovery_safety_ceiling_is_a_boundary_not_inconclusive(tmp_path, capsys) -> None:
    code, summary = _run(
        capsys,
        ["run", "--stress-axis", "cube_count", "--discover-limit", "--ceiling", "2", "--out", str(tmp_path)],
    )
    assert code == cli.EXIT_OK
    assert summary["counts"]["boundary"] == 1 and summary["counts"]["pass"] == 2
    full = json.loads((Path(summary["artifact_dir"]) / "summary.json").read_text())
    assert full["last_observed_pass"] == 2 and full["first_observed_failure"] is None


def test_stress_point_identity_is_independent_of_caller_shell(tmp_path) -> None:
    env_a = {k: v for k, v in os.environ.items() if k not in {"PYTHONHASHSEED", "TZ"}}
    env_b = {**env_a, "PYTHONHASHSEED": "777", "TZ": "America/New_York"}
    identities = []
    for index, environment in enumerate((env_a, env_b)):
        completed = subprocess.run(
            [sys.executable, "-m", "lib_dst", "run", "--stress-axis", "cube_count", "--value", "2",
             "--out", str(tmp_path / str(index))],
            capture_output=True, text=True, check=False, cwd=str(ROOT), env=environment,
        )
        assert completed.returncode == 0, completed.stderr
        identities.append(json.loads(completed.stdout)["identity_sha256"])
    assert identities[0] == identities[1]


def test_subprocess_cli_runs_from_foreign_cwd_with_windows_style_paths(tmp_path) -> None:
    trace_path = _write_trace(tmp_path / "a dir" / "trace one.json", generate_trace(0))
    environment = worker_environment()
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst", "replay", str(trace_path), "--out", str(tmp_path / "out dir")],
        capture_output=True, text=True, check=False, cwd=str(tmp_path), env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert Path(summary["artifact_dir"]).is_relative_to(tmp_path / "out dir")
    assert "[om-dst]" in completed.stderr


def test_om_dst_shell_wrapper_forwards_exit_code(tmp_path) -> None:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX sh is not available")
    completed = subprocess.run(
        [shell, str(ROOT / "om-dst"), "replay", str(tmp_path / "missing.json")],
        capture_output=True, text=True, check=False, cwd=str(tmp_path),
    )
    assert completed.returncode == cli.EXIT_USAGE, completed.stderr
