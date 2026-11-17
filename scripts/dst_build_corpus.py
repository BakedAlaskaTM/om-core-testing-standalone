"""Rebuild the fixed DST pull-request regression corpus in ``tests/dst_corpus``.

Usage (repository root)::

    .venv/Scripts/python.exe scripts/dst_build_corpus.py            # write
    .venv/Scripts/python.exe scripts/dst_build_corpus.py --check    # verify only

Curation policy (see plan.md, Phase 5):

* Entries are chosen for semantic breadth, not volume: every workload profile, every
  scheduler mode, every declared persistence fault, both known command-boundary
  defects, and a few historical mutation-evolved traces.
* Every trace is stored as canonical JSON named by its SHA-256 and pinned in
  ``manifest.json``; ``om-dst run --corpus`` refuses a corpus whose files and manifest
  disagree.
* Generated recipes run under the worker environment (``PYTHONHASHSEED=1``,
  ``TZ=UTC``). The recorded environment still contains the Python version and platform,
  so regenerating on a different interpreter or OS yields different hashes. Regenerate
  only deliberately, review the diff, and commit the manifest with the traces.
* An ``expect`` other than ``passed`` declares a known failure. When OM Core is fixed,
  ``om-dst`` reports it as ``xpass`` (exit 1) so the expectation must be updated here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib_dst.artifacts import atomic_write_text  # noqa: E402
from lib_dst.cli import pinned_generation_environment  # noqa: E402
from lib_dst.faults import FaultDecision, with_faults  # noqa: E402
from lib_dst.generator import generate_trace  # noqa: E402
from lib_dst.profiles import profile_names  # noqa: E402
from lib_dst.scale_generator import (  # noqa: E402
    generate_profile_trace,
    generate_stress_axis_trace,
)
from lib_dst.scheduling import generate_scheduler_scenario, with_scheduler  # noqa: E402
from lib_dst.schemas import TraceTranscript  # noqa: E402

CORPUS = ROOT / "tests" / "dst_corpus"
HISTORICAL = ROOT / ".om" / "dst-corpus-run-001" / "traces"

PASSED = {"verdict": "passed"}


def _engine_error(code: str) -> dict[str, Any]:
    return {
        "verdict": "failed",
        "origin": "engine",
        "kind": "controlled_engine_error",
        "code": code,
    }


Recipe = tuple[str, str, Callable[[], TraceTranscript], dict[str, Any], str]


def recipes() -> list[Recipe]:
    items: list[Recipe] = []
    for seed in (0, 1, 2):
        items.append(
            (f"legacy-seed-{seed}", f"generate_trace({seed})",
             lambda seed=seed: generate_trace(seed), PASSED, "legacy numeric trace")
        )
    items.append(
        ("legacy-div0-seed-5", "generate_trace(5)", lambda: generate_trace(5), PASSED,
         "deliberate #DIV/0! error contract")
    )
    for profile in profile_names():
        items.append(
            (f"profile-{profile}", f"generate_profile_trace(4500, {profile!r})",
             lambda profile=profile: generate_profile_trace(4500, profile), PASSED,
             f"{profile} workload profile")
        )
    for mode in ("delay", "reorder", "timeout"):
        items.append(
            (f"scheduler-{mode}",
             f"with_scheduler(generate_trace(7), generate_scheduler_scenario(7, {mode!r}))",
             lambda mode=mode: with_scheduler(generate_trace(7), generate_scheduler_scenario(7, mode)),
             PASSED, f"virtual scheduler {mode} contract")
        )
    for operation, outcome, code in (
        ("save", "raise", "persistence.save.injected"),
        ("load", "raise", "persistence.load.injected"),
        ("load", "malformed_json", "persistence.load.malformed-json"),
    ):
        items.append(
            (f"fault-{operation}-{outcome}".replace("_", "-"),
             f"with_faults(generate_trace(3), [FaultDecision('persistence', {operation!r}, 1, {outcome!r})])",
             lambda operation=operation, outcome=outcome: with_faults(
                 generate_trace(3), [FaultDecision("persistence", operation, 1, outcome)]
             ),
             _engine_error(code), "declared persistence fault must surface its signature")
        )
    items.append(
        ("stress-dependency-depth-4", "generate_stress_axis_trace(4500, 'dependency_depth', 4)",
         lambda: generate_stress_axis_trace(4500, "dependency_depth", 4), PASSED,
         "small cross-cube dependency chain")
    )
    items.append(
        ("known-defect-udf-create", "generate_stress_axis_trace(4500, 'udf_depth', 1)",
         lambda: generate_stress_axis_trace(4500, "udf_depth", 1), _engine_error("RuntimeError"),
         "KNOWN DEFECT: Engine facade lacks udf_registry (plan.md Phase 4.5)")
    )
    items.append(
        ("known-defect-format-address", "generate_stress_axis_trace(4500, 'format_volume', 2)",
         lambda: generate_stress_axis_trace(4500, "format_volume", 2), _engine_error("invalid-item-key"),
         "KNOWN DEFECT: semantic selectors stored as item-ID keys (plan.md Phase 4.5)")
    )
    return items


# Historical mutation-evolved traces retained from the Phase 4 campaign corpus. They
# are copied byte-for-byte; their recorded environment predates environment pinning.
HISTORICAL_TRACES = {
    "evolved-ast-literal": "024c560c744c928980c404310d9fbab130bc6987e96412b8999dca23cafaaff7",
    "evolved-ast-operator": "0f544e3407ca68ffed3ed2d8dba1722beabf220e1fa6ead79f2177bbddd514b5",
    "evolved-recalculation": "26c0f41dfee232e798017eb10426f089b38c7c20addd8e25d10d3102d62da961",
    "evolved-ast-reference": "9def6bf0fd3bb657fff627848273cceda29e0d8425ef20c8affac4d218a25b14",
    "evolved-hardvalue-round": "4b6900b51a9a462ab2c53f9bbb4a32c3956f97306612d52f89a4a2b52507b7f2",
    "evolved-division-error": "528a1b5c78db30f4613b7b3cb7e7011b85c1a61e7a84a150e93005a36373bab3",
}


def build() -> tuple[dict[str, Any], dict[str, str]]:
    files: dict[str, str] = {}
    entries = []
    with pinned_generation_environment():
        for entry_id, source, make, expect, description in recipes():
            trace = make()
            digest = trace.sha256()
            files[f"traces/{digest}.json"] = trace.to_json()
            entries.append({
                "id": entry_id,
                "trace": f"traces/{digest}.json",
                "trace_sha256": digest,
                "source": source,
                "expect": expect,
                "description": description,
            })
    for entry_id, digest in HISTORICAL_TRACES.items():
        trace = TraceTranscript.from_json((HISTORICAL / f"{digest}.json").read_text(encoding="utf-8"))
        if trace.sha256() != digest:
            raise SystemExit(f"Historical trace {digest} does not match its content hash")
        files[f"traces/{digest}.json"] = trace.to_json()
        entries.append({
            "id": entry_id,
            "trace": f"traces/{digest}.json",
            "trace_sha256": digest,
            "source": f".om/dst-corpus-run-001/traces/{digest}.json",
            "expect": PASSED,
            "description": "Phase 4 mutation-evolved trace",
        })
    manifest = {
        "schema_version": 1,
        "description": "Fixed DST pull-request regression corpus; see scripts/dst_build_corpus.py",
        "entries": entries,
    }
    return manifest, files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()
    manifest, files = build()
    manifest_text = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    if args.check:
        current = (CORPUS / "manifest.json").read_text(encoding="utf-8")
        stale = [path for path, text in files.items()
                 if not (CORPUS / path).is_file() or (CORPUS / path).read_text(encoding="utf-8") != text]
        if current != manifest_text or stale:
            print(f"corpus differs from recipes: manifest_changed={current != manifest_text} stale={stale}")
            return 1
        print(f"corpus matches recipes ({len(manifest['entries'])} entries)")
        return 0
    expected = set(files)
    existing = {f"traces/{path.name}" for path in (CORPUS / "traces").glob("*.json")} if (CORPUS / "traces").is_dir() else set()
    for path, text in sorted(files.items()):
        atomic_write_text(CORPUS / path, text)
    for stale in sorted(existing - expected):
        (CORPUS / stale).unlink()
    atomic_write_text(CORPUS / "manifest.json", manifest_text)
    print(f"wrote {len(manifest['entries'])} entries to {CORPUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
