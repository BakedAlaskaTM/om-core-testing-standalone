"""Coordinator-owned run artifacts with atomic writes, collision checks, and resume.

Layout of one run directory ``<out>/<run-id>/``::

    manifest.json        tool, schema, mode, command, provenance, status
    summary.json         deterministic identity of the results (written last)
    measurements.json    volatile timings and throughput (never identity)
    checkpoints/*.json   completed units of work, used by --resume
    failures/<sha>/      trace.json (original standalone trace), outcome.json,
                         environment.json, diff.json, minimized.json, shrink.json
    stress/<axis>.json   full stress/limit-discovery reports

The manifest status is ``incomplete`` until ``finalize`` succeeds, so an interrupted
run is always distinguishable from a finished one. Only the coordinator process that
opened the directory may write to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


ARTIFACT_SCHEMA_VERSION = 1
TOOL_NAME = "om-dst"
STATUS_INCOMPLETE = "incomplete"
STATUS_COMPLETE = "complete"

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


class RunDirectoryError(RuntimeError):
    """The requested run directory cannot be used as asked."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_text(path: str | Path, value: str) -> None:
    """Write UTF-8 text through a same-directory temporary file and atomic replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_key(value: str) -> str:
    """Filesystem-safe name on every platform (Windows forbids ``:`` and others)."""

    cleaned = _SAFE_KEY.sub("_", value)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"Unusable artifact key: {value!r}")
    return cleaned


def run_id_for(mode: str, command: dict[str, Any]) -> str:
    """Deterministic run identifier: the same command always maps to the same id."""

    return f"{safe_key(mode)}-{sha256_text(canonical_json(command))[:16]}"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class RunDirectory:
    def __init__(self, path: Path, manifest: dict[str, Any]) -> None:
        self.path = path
        self.manifest = manifest
        self._owner_pid = os.getpid()

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @property
    def resumed(self) -> bool:
        return bool(self.manifest.get("resumed", False))

    @classmethod
    def open(
        cls,
        out_dir: str | Path,
        *,
        mode: str,
        command: dict[str, Any],
        provenance: dict[str, Any],
        overwrite: bool = False,
        resume: bool = False,
    ) -> "RunDirectory":
        if overwrite and resume:
            raise RunDirectoryError("--overwrite and --resume are mutually exclusive")
        run_id = run_id_for(mode, command)
        path = Path(out_dir).resolve() / run_id
        manifest_path = path / "manifest.json"
        resumed = False
        if path.exists():
            existing = cls._existing_manifest(path)
            if existing is None:
                if any(path.iterdir()):
                    raise RunDirectoryError(
                        f"{path} exists but is not an {TOOL_NAME} run directory; "
                        "refusing to use or delete it"
                    )
            elif overwrite:
                shutil.rmtree(path)
            elif existing.get("status") == STATUS_COMPLETE:
                raise RunDirectoryError(
                    f"Run {run_id} is already complete at {path}; pass --overwrite to "
                    "replace it"
                )
            elif resume:
                if existing.get("command") != command:
                    raise RunDirectoryError(
                        f"Run {run_id} at {path} was started with a different command"
                    )
                resumed = True
            else:
                raise RunDirectoryError(
                    f"Run {run_id} at {path} is incomplete; pass --resume to continue "
                    "it or --overwrite to restart it"
                )
        manifest = {
            "tool": TOOL_NAME,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "mode": mode,
            "command": command,
            "provenance": provenance,
            "status": STATUS_INCOMPLETE,
            "resumed": resumed,
            "exit_code": None,
        }
        directory = cls(path, manifest)
        path.mkdir(parents=True, exist_ok=True)
        atomic_write_text(manifest_path, _pretty(manifest))
        return directory

    @staticmethod
    def _existing_manifest(path: Path) -> dict[str, Any] | None:
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            value = _read_json(manifest_path)
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("tool") != TOOL_NAME:
            return None
        return value

    def _require_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("Only the coordinator process may write run artifacts")

    def write_json(self, relative: str, value: Any, *, pretty: bool = True) -> Path:
        self._require_owner()
        target = self.path / relative
        atomic_write_text(target, _pretty(value) if pretty else canonical_json(value))
        return target

    def write_text(self, relative: str, value: str) -> Path:
        self._require_owner()
        target = self.path / relative
        atomic_write_text(target, value)
        return target

    def checkpoint(self, key: str, value: Any) -> None:
        self.write_json(f"checkpoints/{safe_key(key)}.json", value, pretty=False)

    def load_checkpoint(self, key: str) -> Any | None:
        target = self.path / "checkpoints" / f"{safe_key(key)}.json"
        if not target.is_file():
            return None
        try:
            return _read_json(target)
        except ValueError:
            # A torn checkpoint cannot exist with atomic replace; treat a corrupt one
            # as absent so the unit of work is recomputed rather than trusted.
            return None

    def failure_dir(self, trace_sha256: str) -> str:
        return f"failures/{safe_key(trace_sha256)}"

    def write_failure(
        self,
        *,
        trace_json: str,
        outcome: dict[str, Any],
        environment: dict[str, Any],
        diff: dict[str, Any] | None = None,
    ) -> str:
        """Retain the original standalone trace plus its evidence. Returns rel dir."""

        trace_sha256 = sha256_text(trace_json)
        relative = self.failure_dir(trace_sha256)
        # The trace is written byte-for-byte as canonical JSON so its file hash equals
        # TraceTranscript.sha256() and it replays without any other artifact.
        self.write_text(f"{relative}/trace.json", trace_json)
        self.write_json(f"{relative}/outcome.json", outcome)
        self.write_json(f"{relative}/environment.json", environment)
        if diff is not None:
            self.write_json(f"{relative}/diff.json", diff)
        return relative

    def finalize(
        self,
        *,
        summary: dict[str, Any],
        measurements: dict[str, Any],
        exit_code: int,
    ) -> None:
        self.write_json("measurements.json", measurements)
        self.write_json("summary.json", summary)
        self.manifest = {**self.manifest, "status": STATUS_COMPLETE, "exit_code": exit_code}
        self.write_json("manifest.json", self.manifest)


def _pretty(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2) + "\n"
