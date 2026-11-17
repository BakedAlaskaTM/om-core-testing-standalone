"""Canonical, ID-independent observations of selected workspace state."""

from __future__ import annotations

import math
from typing import Any

from lib_dst.schemas import CanonicalObservation, ObservationSpec
from lib_openm.rule_eval.utils import CellError
from lib_openm.technical_ids import AT_ID_TO_CHANNEL


class ObservationAddressError(RuntimeError):
    """Engine state contains a cube address that is not keyed by item IDs."""

    def __init__(self, cube: str, dimension_id: str, item_key: str) -> None:
        self.cube = cube
        self.dimension_id = dimension_id
        self.item_key = item_key
        super().__init__(
            f"Cube {cube!r} contains unknown item key {item_key!r} "
            f"for dimension {dimension_id!r}"
        )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, CellError):
        return {"error": value.code}
    if isinstance(value, float):
        if math.isnan(value):
            return {"number": "NaN"}
        if math.isinf(value):
            return {"number": "Infinity" if value > 0 else "-Infinity"}
        if value == 0:
            return 0.0
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def observe_workspace(workspace: Any, spec: ObservationSpec) -> CanonicalObservation:
    dimensions_by_name = {dim.name: dim for dim in workspace.dimensions.values()}
    cubes_by_name = {cube.name: cube for cube in workspace.cubes.values()}
    sampled_addresses: dict[str, set[tuple[str, ...]]] = {}
    for cell in spec.cells:
        sampled_addresses.setdefault(cell.cube, set()).add(tuple(cell.address))

    dimensions: list[dict[str, Any]] = []
    for name in sorted(spec.dimensions):
        dimension = dimensions_by_name[name]
        dimensions.append(
            {
                "name": dimension.name,
                "type": dimension.dim_type,
                "items": [item.name for item in dimension.items],
            }
        )

    cubes: list[dict[str, Any]] = []
    for name in sorted(spec.cubes):
        cube = cubes_by_name[name]
        item_names = {
            dim_id: {item.id: item.name for item in workspace.dimensions[dim_id].items}
            for dim_id in cube.dimension_ids
            if dim_id != "@"
        }
        dimension_names = [
            "@" if dim_id == "@" else workspace.dimensions[dim_id].name
            for dim_id in cube.dimension_ids
        ]
        values = []
        for address, value in cube.data.items():
            logical_address = []
            for dim_id, item_id in zip(cube.dimension_ids, address):
                if dim_id == "@":
                    channel = AT_ID_TO_CHANNEL.get(item_id, item_id)
                    logical_address.append(f"@.{channel}")
                else:
                    try:
                        logical_address.append(item_names[dim_id][item_id])
                    except KeyError as exc:
                        raise ObservationAddressError(
                            cube.name, dim_id, str(item_id)
                        ) from exc
            if name in sampled_addresses and tuple(logical_address) not in sampled_addresses[name]:
                continue
            values.append(
                {
                    "address": logical_address,
                    "value": _canonical_value(value),
                    "user_override": address in cube.user_override_addrs,
                }
            )
        values.sort(key=lambda entry: tuple(entry["address"]))
        cubes.append(
            {
                "name": cube.name,
                "dimensions": dimension_names,
                "values": values,
            }
        )

    selected_cubes = set(spec.cubes)
    rule_ids = [rule_id for rule_id in workspace.rule_order if rule_id in workspace.rules]
    rule_ids.extend(sorted(set(workspace.rules) - set(rule_ids)))
    rules: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        rule = workspace.rules[rule_id]
        cube = workspace.cubes.get(rule.cube_id)
        if cube is None or cube.name not in selected_cubes:
            continue
        rules.append(
            {
                "cube": cube.name,
                "targets": [list(target) for target in (rule.targets or ())],
                "expression": rule.expression,
                "is_anchored": rule.is_anchored,
                "precedence": len(rules),
            }
        )

    return CanonicalObservation(
        state={"dimensions": dimensions, "cubes": cubes, "rules": rules}
    )
