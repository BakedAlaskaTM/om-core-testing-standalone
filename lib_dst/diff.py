"""Deterministic structural diff of canonical JSON values."""

from __future__ import annotations

from typing import Any


DEFAULT_DIFF_LIMIT = 500


def _walk(expected: Any, actual: Any, path: str, out: list[dict[str, Any]]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{path}.{key}"
            if key not in actual:
                out.append({"path": child, "change": "removed", "expected": expected[key]})
            elif key not in expected:
                out.append({"path": child, "change": "added", "actual": actual[key]})
            else:
                _walk(expected[key], actual[key], child, out)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(actual):
                out.append({"path": child, "change": "removed", "expected": expected[index]})
            elif index >= len(expected):
                out.append({"path": child, "change": "added", "actual": actual[index]})
            else:
                _walk(expected[index], actual[index], child, out)
        return
    # bool is an int subclass; compare type identity so True != 1 is reported.
    if type(expected) is not type(actual) or expected != actual:
        out.append(
            {"path": path, "change": "changed", "expected": expected, "actual": actual}
        )


def canonical_diff(
    expected: Any,
    actual: Any,
    *,
    expected_label: str = "expected",
    actual_label: str = "actual",
    limit: int = DEFAULT_DIFF_LIMIT,
) -> dict[str, Any]:
    """Return every differing leaf path in a stable order, truncated to ``limit``."""

    differences: list[dict[str, Any]] = []
    _walk(expected, actual, "$", differences)
    return {
        "expected_label": expected_label,
        "actual_label": actual_label,
        "equal": not differences,
        "difference_count": len(differences),
        "truncated": len(differences) > limit,
        "differences": differences[:limit],
    }
