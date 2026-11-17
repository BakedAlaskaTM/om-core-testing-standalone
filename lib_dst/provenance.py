"""Run provenance: repository revision, dependency versions, and worker environment.

Everything here describes *where* a deterministic result was observed. None of it is
part of a trace or report identity hash; it is recorded beside those identities so a
replay can warn about, or reject, an incompatible environment.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from lib_dst.schemas import EnvironmentDescriptor


WORKER_PYTHON_HASH_SEED = "1"
WORKER_TIMEZONE = "UTC"

# Packages whose versions can plausibly change engine-visible numeric or persistence
# behavior. The list is descriptive evidence, not a lock file.
TRACKED_DEPENDENCIES = ("gmpy2", "msgpack", "numpy", "pymoo", "PySide6", "scipy")

# Environment-descriptor fields compared on replay. All of them are recorded in schema-1
# traces; any difference means a replay result is not a determinism claim for the
# recorded environment.
COMPATIBILITY_FIELDS = (
    "python",
    "implementation",
    "platform",
    "python_hash_seed",
    "timezone",
    "engine_mode",
    "recalculation",
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _git_revision(root: str) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not head:
        return "unknown"
    return f"{head}-dirty" if dirty else head


def om_core_revision() -> str:
    """Return the OM Core revision, preferring an explicit coordinator value.

    Workers receive ``OM_CORE_REVISION`` from their coordinator so every point in one
    run reports the same revision without each worker shelling out to git.
    """

    explicit = os.environ.get("OM_CORE_REVISION")
    if explicit:
        return explicit
    return _git_revision(str(repository_root()))


def dependency_versions(
    names: tuple[str, ...] = TRACKED_DEPENDENCIES,
) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "absent"
    return versions


def pin_generation_environment(
    python_hash_seed: str = WORKER_PYTHON_HASH_SEED,
    timezone: str = WORKER_TIMEZONE,
) -> None:
    """Make coordinator-generated traces record the environment their workers use.

    Trace generators capture ``EnvironmentDescriptor.current()``, which reads these
    variables. ``PYTHONHASHSEED`` cannot change the running coordinator's own hash
    randomization; generation itself does not depend on it. The pin only makes the
    recorded descriptor (and therefore the trace hash) independent of the caller's
    shell and identical to the clean worker that executes the trace.
    """

    os.environ["PYTHONHASHSEED"] = python_hash_seed
    os.environ["TZ"] = timezone


def worker_environment(
    *,
    python_hash_seed: str = WORKER_PYTHON_HASH_SEED,
    revision: str | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for a clean worker that can import ``lib_dst`` from any cwd."""

    environment = dict(os.environ if base is None else base)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = WORKER_TIMEZONE
    root = str(repository_root())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = root if not existing else os.pathsep.join((root, existing))
    environment["OM_CORE_REVISION"] = revision or om_core_revision()
    return environment


def provenance_record() -> dict[str, Any]:
    """Describe the coordinator. Recorded in manifests, never in identities."""

    return {
        "om_core_revision": om_core_revision(),
        "dependencies": dependency_versions(),
        "coordinator_environment": EnvironmentDescriptor.current().to_dict(),
        "worker_environment": {
            "PYTHONHASHSEED": WORKER_PYTHON_HASH_SEED,
            "TZ": WORKER_TIMEZONE,
        },
        "python_executable": sys.executable,
    }


def environment_mismatches(
    recorded: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return every compatibility field whose recorded and observed values differ."""

    mismatches = []
    for field in COMPATIBILITY_FIELDS:
        if recorded.get(field) != observed.get(field):
            mismatches.append(
                {
                    "field": field,
                    "recorded": recorded.get(field),
                    "observed": observed.get(field),
                }
            )
    return mismatches
