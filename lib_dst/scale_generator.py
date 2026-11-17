"""Deterministic typed topology generation for Phase 4.5 profiles."""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from typing import Any, Iterable

from lib_dst.ast import Binary, Call, Number, NumericExpr, Reference, render, to_dict
from lib_dst.oracle import canonical, evaluate
from lib_dst.profiles import WorkloadMetrics, WorkloadProfile, resolve_profile
from lib_dst.schemas import (
    EnvironmentDescriptor,
    ObservationCell,
    ObservationSpec,
    TraceOperation,
    TraceTranscript,
)
from lib_dst.seeding import SeedStreams
from lib_dst.symbols import Symbol, SymbolKind, SymbolTable
from lib_openm.rule_eval import _Parser, _tokenise


GENERATOR_NAME = "phase45.scale.v1"
_SEED_DOMAINS = (
    "scale.topology",
    "scale.cardinality",
    "scale.rule_graph",
    "scale.value_density",
    "scale.operations",
    "scale.observation",
    "runtime.clock",
    "runtime.ids",
    "runtime.random",
)


class WorkloadBudgetError(ValueError):
    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        super().__init__("Workload exceeds structural budget: " + ", ".join(self.violations))


def _operation(
    operations: list[TraceOperation], kind: str, target: str, args: dict[str, Any]
) -> TraceOperation:
    operation = TraceOperation(len(operations), kind, target, args)
    operations.append(operation)
    return operation


def _address_for_index(
    dimension_handles: tuple[str, ...],
    items: dict[str, tuple[str, ...]],
    index: int,
) -> tuple[str, ...]:
    result: list[str] = []
    remainder = index
    for dimension in reversed(dimension_handles):
        choices = items[dimension]
        result.append(choices[remainder % len(choices)])
        remainder //= len(choices)
    return tuple(reversed(result))


def _logical_address(
    handles: tuple[str, ...], symbols: SymbolTable
) -> tuple[str, ...]:
    return ("@.value", *(symbols.require(handle, SymbolKind.ITEM).name for handle in handles))


def _targets(handles: tuple[str, ...], symbols: SymbolTable) -> list[list[str]]:
    result = []
    for handle in handles:
        item = symbols.require(handle, SymbolKind.ITEM)
        dimension = symbols.require(str(item.owner), SymbolKind.DIMENSION)
        result.append([dimension.name, item.name])
    return result


def _ast_node_count(value: dict[str, Any] | None) -> int:
    if value is None:
        return 0
    count = 0
    pending = [value]
    while pending:
        node = pending.pop()
        count += 1
        node_type = node.get("type")
        if node_type in {"binary", "comparison"}:
            pending.extend((node["left"], node["right"]))
        elif node_type == "negate":
            pending.append(node["operand"])
        elif node_type == "call":
            pending.extend(node["arguments"])
    return count


def _nested_abs(expression: NumericExpr, depth: int) -> NumericExpr:
    for _ in range(max(0, depth)):
        expression = Call("ABS", (expression,))
    return expression


def _known_text_expression(value: int, depth: int) -> tuple[str, dict[str, Any], None]:
    expression = "ABS(" * depth + str(value) + ")" * depth
    _Parser(_tokenise(expression)).parse()
    return expression, canonical(evaluate(Number(abs(value)), {})), None


def _outline(
    dimension: str,
    item_handles: tuple[str, ...],
    depth: int,
    symbols: SymbolTable,
) -> list[dict[str, Any]]:
    leaves = [
        {"label": symbols.require(handle, SymbolKind.ITEM).name, "item": handle, "children": []}
        for handle in item_handles
    ]
    if depth <= 0:
        return leaves
    node: dict[str, Any] = {"label": f"{dimension}_Group_{depth}", "children": leaves}
    for level in range(depth - 1, 0, -1):
        node = {"label": f"{dimension}_Group_{level}", "children": [node]}
    return [node]


def generate_profile_trace(
    master_seed: int | str,
    profile: str | WorkloadProfile = "small",
    *,
    overrides: dict[str, int] | None = None,
    stress_axis: str | None = None,
    stress_value: int | None = None,
    seed_overrides: dict[str, int] | None = None,
) -> TraceTranscript:
    """Generate a fully resolved and replayable profile trace."""

    resolved = resolve_profile(profile, overrides=overrides)
    streams = SeedStreams(master_seed)
    seed_overrides = dict(seed_overrides or {})

    def domain_seed(domain: str) -> int:
        return int(seed_overrides.get(domain, streams.seed(domain)))

    topology_rng = random.Random(domain_seed("scale.topology"))
    value_rng = random.Random(domain_seed("scale.value_density"))
    graph_rng = random.Random(domain_seed("scale.rule_graph"))
    observation_rng = random.Random(domain_seed("scale.observation"))

    symbols = SymbolTable()
    operations: list[TraceOperation] = []
    dependency_enabled = resolved.name == "dependency_deep" or stress_axis in {
        "dependency_depth",
        "dependency_fanout",
    }
    _operation(operations, "configure_serial", "runtime", {"enabled": False})

    dimension_handles: list[str] = []
    items: dict[str, tuple[str, ...]] = {}
    for dimension_index in range(resolved.dimensions):
        dimension_handle = f"dimension:scale/{dimension_index}"
        dimension_name = f"ScaleDim{dimension_index:03d}"
        symbols.declare(Symbol(dimension_handle, SymbolKind.DIMENSION, dimension_name))
        _operation(
            operations,
            "create_dimension",
            dimension_handle,
            {"name": dimension_name, "dim_type": "set"},
        )
        dimension_items: list[str] = []
        for item_index in range(resolved.items_per_dimension):
            item_handle = f"item:scale/{dimension_index}/{item_index}"
            item_name = f"Item{item_index:04d}"
            symbols.declare(
                Symbol(item_handle, SymbolKind.ITEM, item_name, dimension_handle)
            )
            _operation(
                operations,
                "create_dimension_item",
                item_handle,
                {"dimension": dimension_handle, "name": item_name},
            )
            dimension_items.append(item_handle)
        dimension_handles.append(dimension_handle)
        items[dimension_handle] = tuple(dimension_items)

    if resolved.hierarchy_depth:
        for dimension_handle in dimension_handles:
            dimension_name = symbols.require(dimension_handle).name
            _operation(
                operations,
                "set_dimension_outline",
                dimension_handle,
                {
                    "outline": _outline(
                        dimension_name,
                        items[dimension_handle],
                        resolved.hierarchy_depth,
                        symbols,
                    )
                },
            )

    for udf_index in range(resolved.udf_depth):
        handle = f"udf:scale/{udf_index}"
        name = f"DST_UDF_{udf_index:04d}"
        expression = "ABS(x)" if udf_index == 0 else f"DST_UDF_{udf_index - 1:04d}(x) + 1"
        symbols.declare(Symbol(handle, SymbolKind.UDF, name))
        _operation(
            operations,
            "create_udf",
            handle,
            {"name": name, "params": ["x"], "expression": expression},
        )

    cube_definitions: list[tuple[str, str, tuple[str, ...]]] = []
    for cube_index in range(resolved.cubes):
        cube_handle = f"cube:scale/{cube_index}"
        cube_name = f"ScaleCube{cube_index:04d}"
        if dependency_enabled:
            cube_dimensions = (dimension_handles[0],)
        else:
            start = topology_rng.randrange(len(dimension_handles))
            cube_dimensions = tuple(
                dimension_handles[(start + offset) % len(dimension_handles)]
                for offset in range(resolved.dimensions_per_cube)
            )
        symbols.declare(Symbol(cube_handle, SymbolKind.CUBE, cube_name))
        _operation(
            operations,
            "create_cube",
            cube_handle,
            {"name": cube_name, "dimensions": list(cube_dimensions)},
        )
        cube_definitions.append((cube_handle, cube_name, cube_dimensions))

    probes: list[dict[str, Any]] = []
    rule_asts: list[dict[str, Any]] = []
    sampled: set[tuple[str, tuple[str, ...]]] = set()
    mandatory_samples: set[tuple[str, tuple[str, ...]]] = set()
    dependency_edges = 0
    previous_cube_target: tuple[str, str, str, dict[str, Any]] | None = None

    for cube_index, (cube_handle, cube_name, cube_dimensions) in enumerate(cube_definitions):
        addressable = resolved.items_per_dimension ** len(cube_dimensions)
        required = resolved.rules_per_cube + resolved.populated_cells_per_cube
        if required > addressable:
            raise WorkloadBudgetError(
                (f"cube-addresses:{required}>{addressable}",)
            )

        hard_values: dict[tuple[str, ...], int] = {}
        hard_count = min(resolved.populated_cells_per_cube, addressable - resolved.rules_per_cube)
        for hard_index in range(hard_count):
            address = _address_for_index(
                cube_dimensions, items, resolved.rules_per_cube + hard_index
            )
            value = value_rng.randint(-50, 50)
            hard_values[address] = value
            _operation(
                operations,
                "set_cell_hardvalue",
                cube_handle,
                {"address": list(address), "value": value},
            )
            sampled.add((cube_name, _logical_address(address, symbols)))
            if hard_index in {0, hard_count - 1}:
                mandatory_samples.add((cube_name, _logical_address(address, symbols)))

        for format_index in range(min(resolved.format_cells_per_cube, addressable)):
            address = _address_for_index(cube_dimensions, items, format_index)
            _operation(
                operations,
                "set_cell_channel",
                cube_handle,
                {"address": list(address), "channel": "bold", "value": True},
            )
            sampled.add(
                (
                    cube_name,
                    (
                        "@.font_weight",
                        *(symbols.require(handle).name for handle in address),
                    ),
                )
            )

        prior_rule: tuple[str, dict[str, Any]] | None = None
        fanout_source: tuple[str, dict[str, Any]] | None = None
        for rule_index in range(resolved.rules_per_cube):
            target_handles = _address_for_index(cube_dimensions, items, rule_index)
            target = {
                "cube": cube_name,
                "address": list(_logical_address(target_handles, symbols)),
            }
            ast: NumericExpr | None = None
            expected: dict[str, Any]

            if len(cube_dimensions) != 1:
                literal = graph_rng.randint(-20, 20)
                ast = Number(literal)
                expression = render(ast)
                expected = canonical(evaluate(ast, {}))
            elif (
                resolved.udf_depth
                and cube_index == len(cube_definitions) - 1
                and rule_index == resolved.rules_per_cube - 1
            ):
                value = graph_rng.randint(-20, 20)
                expression = f"DST_UDF_{resolved.udf_depth - 1:04d}({value})"
                expected = {"number": float(abs(value) + max(0, resolved.udf_depth - 1))}
                dependency_edges += resolved.udf_depth
            elif (
                dependency_enabled
                and previous_cube_target is not None
                and rule_index == 0
            ):
                previous_handle, previous_cube, previous_dimension, previous_expected = previous_cube_target
                own_address = next(iter(hard_values))
                own_handle = own_address[0]
                ast = Binary(
                    "+",
                    Reference(previous_handle, previous_cube, previous_dimension, symbols.require(previous_handle).name),
                    Reference(own_handle, cube_name, symbols.require(cube_dimensions[0]).name, symbols.require(own_handle).name),
                )
                values = {
                    previous_handle: int(float(previous_expected["number"])),
                    own_handle: hard_values[own_address],
                }
                expression = render(ast)
                expected = canonical(evaluate(ast, values))
                dependency_edges += 2
            elif prior_rule is not None:
                source_rule = (
                    fanout_source
                    if dependency_enabled
                    and fanout_source is not None
                    and rule_index <= resolved.dependency_fanout
                    else prior_rule
                )
                prior_handle, prior_expected = source_rule
                ast = Binary(
                    "+",
                    Reference(
                        prior_handle,
                        cube_name,
                        symbols.require(cube_dimensions[0]).name,
                        symbols.require(prior_handle).name,
                    ),
                    Number(1),
                )
                expression = render(ast)
                expected = canonical(evaluate(ast, {prior_handle: int(float(prior_expected["number"]))}))
                dependency_edges += 1
            else:
                input_address = next(iter(hard_values))
                input_handle = input_address[0]
                base: NumericExpr = Reference(
                    input_handle,
                    cube_name,
                    symbols.require(cube_dimensions[0]).name,
                    symbols.require(input_handle).name,
                )
                if stress_axis == "builtin_nesting" and stress_value is not None:
                    expression, expected, ast = _known_text_expression(
                        hard_values[input_address], stress_value
                    )
                else:
                    base = _nested_abs(base, resolved.ast_depth)
                    width = max(1, resolved.expression_width)
                    if width <= 256:
                        ast = Call("SUM", (base, *(Number(1) for _ in range(width - 1))))
                        expression = render(ast)
                        expected = canonical(
                            evaluate(ast, {input_handle: hard_values[input_address]})
                        )
                    else:
                        rendered_base = render(base)
                        expression = "SUM(" + ", ".join(
                            [rendered_base, *("1" for _ in range(width - 1))]
                        ) + ")"
                        expected = {
                            "number": float(abs(hard_values[input_address]) + width - 1)
                        }
                        ast = None

            _Parser(_tokenise(expression)).parse()
            operation = _operation(
                operations,
                "set_rule",
                cube_handle,
                {
                    "targets": _targets(target_handles, symbols),
                    "expression": expression,
                    "is_anchored": True,
                },
            )
            ast_value = to_dict(ast) if ast is not None else None
            rule_asts.append(
                {
                    "step": operation.step,
                    "cube": cube_name,
                    "target": target,
                    "expression": expression,
                    "ast": ast_value,
                    "expected": expected,
                }
            )
            probes.append({"target": target, "expected": expected})
            sampled.add((cube_name, tuple(target["address"])))
            mandatory_samples.add((cube_name, tuple(target["address"])))
            prior_rule = (target_handles[0], expected)
            if fanout_source is None:
                fanout_source = prior_rule

        if prior_rule is not None:
            previous_cube_target = (
                prior_rule[0],
                cube_name,
                symbols.require(cube_dimensions[0]).name,
                prior_rule[1],
            )

        for view_index in range(resolved.views_per_cube):
            view_handle = f"view:scale/{cube_index}/{view_index}"
            view_name = f"ScaleView{cube_index:04d}_{view_index:03d}"
            symbols.declare(Symbol(view_handle, SymbolKind.VIEW, view_name, cube_handle))
            _operation(
                operations,
                "create_view",
                view_handle,
                {
                    "name": view_name,
                    "cube": cube_handle,
                    "rows": [cube_dimensions[0]],
                    "cols": list(cube_dimensions[1:2]),
                    "pages": list(cube_dimensions[2:]),
                },
            )

    for churn_index in range(resolved.model_churn):
        cube_handle = f"cube:churn/{churn_index}"
        cube_name = f"ChurnCube{churn_index:04d}"
        symbols.declare(Symbol(cube_handle, SymbolKind.CUBE, cube_name))
        _operation(
            operations,
            "create_cube",
            cube_handle,
            {"name": cube_name, "dimensions": [dimension_handles[0]]},
        )
        _operation(operations, "delete_cube", cube_handle, {})

    for _ in range(max(1, resolved.event_recalculations)):
        _operation(operations, "run_recalculation", "runtime", {"scope": "all"})

    # Important cells are mandatory. Additional materialized cells are sampled in a
    # seed-derived order so future population growth does not depend on dict ordering.
    sampled_list = sorted(sampled)
    observation_rng.shuffle(sampled_list)
    per_cube: dict[str, list[tuple[str, ...]]] = {}
    for cube_name, address in sampled_list:
        values = per_cube.setdefault(cube_name, [])
        if (
            len(values) < resolved.observation_samples_per_cube
            or (cube_name, address) in mandatory_samples
        ):
            values.append(address)
    cells = tuple(
        ObservationCell(cube_name, address)
        for cube_name in sorted(per_cube)
        for address in sorted(set(per_cube[cube_name]))
    )

    addressable_cells = sum(
        resolved.items_per_dimension ** len(cube_dimensions)
        for _, _, cube_dimensions in cube_definitions
    )
    populated_cells = sum(
        1
        for operation in operations
        if operation.kind in {"set_cell_hardvalue", "set_cell_channel", "set_rule"}
    )
    ast_nodes = sum(_ast_node_count(entry["ast"]) for entry in rule_asts)
    estimated_persistence = (
        resolved.dimensions * resolved.items_per_dimension * 96
        + addressable_cells // 8
        + populated_cells * 192
        + len(operations) * 128
    )
    virtual_events = len(operations) * 2 + resolved.event_recalculations
    metrics = WorkloadMetrics(
        dimensions=resolved.dimensions,
        items=resolved.dimensions * resolved.items_per_dimension,
        max_items_per_dimension=resolved.items_per_dimension,
        addressable_cells=addressable_cells,
        populated_cells=populated_cells,
        cubes=resolved.cubes,
        rules=len(rule_asts),
        ast_nodes=ast_nodes,
        dependency_edges=dependency_edges,
        operations=len(operations),
        estimated_persistence_bytes=estimated_persistence,
        virtual_events=virtual_events,
    )
    violations = metrics.budget_violations(resolved.budgets)
    if violations:
        raise WorkloadBudgetError(violations)

    primary = probes[0]
    primary_ast = next((entry["ast"] for entry in rule_asts if entry["ast"] is not None), None)
    metadata: dict[str, Any] = {
        "generator": GENERATOR_NAME,
        "master_seed": master_seed,
        "subseeds": {domain: f"{domain_seed(domain):032x}" for domain in _SEED_DOMAINS},
        "symbols": symbols.to_dict(),
        "oracle": {
            "target": primary["target"],
            "expected": primary["expected"],
            "probes": probes[1:],
        },
        "rule_asts": rule_asts,
        "features": sorted(
            {
                f"profile:{resolved.name}",
                f"scale:cubes:{resolved.cubes}",
                f"scale:rules:{len(rule_asts)}",
                f"scale:dimensions:{resolved.dimensions}",
                *( [f"stress-axis:{stress_axis}"] if stress_axis else [] ),
            }
        ),
        "workload": {
            "schema_version": 1,
            "profile": resolved.to_dict(),
            "metrics": metrics.to_dict(),
            "stress_axis": stress_axis,
            "stress_value": stress_value,
            "persistence_cycles": resolved.persistence_cycles,
            "observation_seed_domain": "scale.observation",
            "seed_overrides": {
                domain: f"{value:032x}" for domain, value in sorted(seed_overrides.items())
            },
        },
    }
    if primary_ast is not None:
        metadata["ast"] = primary_ast
    if resolved.name == "event_heavy" or stress_axis == "event_volume":
        metadata["scheduler"] = {"decisions": []}

    return TraceTranscript(
        environment=EnvironmentDescriptor.current(),
        operations=tuple(operations),
        observe=ObservationSpec(
            dimensions=tuple(symbols.require(handle).name for handle in dimension_handles),
            cubes=tuple(cube_name for _, cube_name, _ in cube_definitions),
            cells=cells,
        ),
        metadata=metadata,
    )


def generate_stress_axis_trace(
    master_seed: int | str,
    axis: str,
    value: int,
) -> TraceTranscript:
    if value < 1:
        raise ValueError("Stress-axis values must be positive")
    overrides: dict[str, int]
    if axis in {"ast_depth", "trace_ast_depth"}:
        overrides = {"ast_depth": value, "expression_width": 1, "rules_per_cube": 1}
    elif axis == "builtin_nesting":
        overrides = {"ast_depth": 0, "expression_width": 1, "rules_per_cube": 1}
    elif axis == "expression_width":
        overrides = {"expression_width": value, "ast_depth": 0, "rules_per_cube": 1}
    elif axis == "udf_depth":
        overrides = {"udf_depth": value, "rules_per_cube": 1}
    elif axis == "cube_count":
        overrides = {"cubes": value, "dependency_depth": 0}
    elif axis == "rule_count":
        side = max(4, int(value**0.5) + 2)
        overrides = {
            "rules_per_cube": value,
            "items_per_dimension": side,
            "dimensions": 2,
            "dimensions_per_cube": 2,
            "populated_cells_per_cube": 1,
        }
    elif axis == "dimension_count":
        overrides = {"dimensions": value, "dimensions_per_cube": 1}
    elif axis == "dimensions_per_cube":
        overrides = {"dimensions": value, "dimensions_per_cube": value}
    elif axis == "items_per_dimension":
        overrides = {"items_per_dimension": value}
    elif axis == "hierarchy_depth":
        overrides = {"hierarchy_depth": value}
    elif axis == "dependency_depth":
        overrides = {"cubes": value, "dependency_depth": value, "rules_per_cube": 1}
    elif axis == "dependency_fanout":
        overrides = {
            "dependency_fanout": value,
            "rules_per_cube": value + 1,
            "items_per_dimension": value + 4,
        }
    elif axis == "populated_cells":
        overrides = {
            "populated_cells_per_cube": value,
            "items_per_dimension": value + 4,
        }
    elif axis == "persistence_cycles":
        overrides = {"persistence_cycles": value}
    elif axis == "event_volume":
        overrides = {"event_recalculations": value}
    elif axis == "view_count":
        overrides = {"views_per_cube": value}
    elif axis == "format_volume":
        overrides = {
            "format_cells_per_cube": value,
            "items_per_dimension": value + 4,
            "dimensions": 2,
            "dimensions_per_cube": 2,
        }
    elif axis == "model_churn":
        overrides = {"model_churn": value}
    elif axis == "hierarchy_breadth":
        overrides = {"hierarchy_depth": 1, "items_per_dimension": value}
    elif axis == "recalculation_churn":
        overrides = {"event_recalculations": value}
    else:
        raise ValueError(f"Unknown stress axis: {axis}")
    return generate_profile_trace(
        master_seed,
        "small",
        overrides=overrides,
        stress_axis=axis,
        stress_value=value,
    )


def regenerate_scaled_trace(trace: TraceTranscript, divisor: int) -> TraceTranscript:
    """Regenerate a profile trace at a smaller scale for failure shrinking."""

    if divisor < 2:
        raise ValueError("Scale divisor must be at least two")
    workload = trace.metadata.get("workload")
    if not isinstance(workload, dict):
        raise ValueError("Trace has no resolved workload")
    profile = WorkloadProfile.from_dict(workload["profile"])
    reduced = profile.with_values(
        dimensions=max(1, profile.dimensions // divisor),
        items_per_dimension=max(2, profile.items_per_dimension // divisor),
        cubes=max(1, profile.cubes // divisor),
        dimensions_per_cube=max(
            1,
            min(
                max(1, profile.dimensions // divisor),
                profile.dimensions_per_cube // divisor,
            ),
        ),
        rules_per_cube=max(1, profile.rules_per_cube // divisor),
        populated_cells_per_cube=max(1, profile.populated_cells_per_cube // divisor),
        ast_depth=profile.ast_depth // divisor,
        expression_width=max(1, profile.expression_width // divisor),
        dependency_depth=profile.dependency_depth // divisor,
        dependency_fanout=max(1, profile.dependency_fanout // divisor),
        udf_depth=profile.udf_depth // divisor,
        hierarchy_depth=profile.hierarchy_depth // divisor,
        views_per_cube=profile.views_per_cube // divisor,
        format_cells_per_cube=profile.format_cells_per_cube // divisor,
        model_churn=profile.model_churn // divisor,
        persistence_cycles=max(1, profile.persistence_cycles // divisor),
        event_recalculations=max(1, profile.event_recalculations // divisor),
        observation_samples_per_cube=max(1, profile.observation_samples_per_cube // divisor),
    )
    generated = generate_profile_trace(
        trace.metadata.get("master_seed", 0),
        reduced,
        stress_axis=workload.get("stress_axis"),
        stress_value=workload.get("stress_value"),
        seed_overrides={
            domain: int(value, 16)
            for domain, value in workload.get("seed_overrides", {}).items()
        },
    )
    metadata = copy.deepcopy(generated.metadata)
    for key in ("faults", "scheduler"):
        if key in trace.metadata:
            metadata[key] = copy.deepcopy(trace.metadata[key])
    return replace(generated, metadata=metadata)


def refresh_workload_metadata(trace: TraceTranscript) -> TraceTranscript:
    """Recompute realized metrics after a recorded topology mutation."""

    workload = trace.metadata.get("workload")
    if not isinstance(workload, dict):
        return trace
    metadata = copy.deepcopy(trace.metadata)
    symbols = metadata.get("symbols", {})
    item_counts: dict[str, int] = {}
    for value in symbols.values():
        if value.get("kind") == SymbolKind.ITEM.value:
            owner = str(value.get("owner"))
            item_counts[owner] = item_counts.get(owner, 0) + 1
    cubes = [operation for operation in trace.operations if operation.kind == "create_cube"]
    addressable = sum(
        _product(item_counts[str(handle)] for handle in operation.args["dimensions"])
        for operation in cubes
    )
    rule_asts = metadata.get("rule_asts", ())
    ast_nodes = sum(_ast_node_count(entry.get("ast")) for entry in rule_asts)
    dependency_edges = sum(
        _ast_reference_count(entry.get("ast")) for entry in rule_asts
    ) + int(metadata.get("workload", {}).get("profile", {}).get("udf_depth", 0))
    populated = sum(
        operation.kind in {"set_cell_hardvalue", "set_cell_channel", "set_rule"}
        for operation in trace.operations
    )
    dimensions = sum(
        operation.kind == "create_dimension" for operation in trace.operations
    )
    metrics = WorkloadMetrics(
        dimensions=dimensions,
        items=sum(item_counts.values()),
        max_items_per_dimension=max(item_counts.values(), default=0),
        addressable_cells=addressable,
        populated_cells=populated,
        cubes=len(cubes),
        rules=sum(operation.kind == "set_rule" for operation in trace.operations),
        ast_nodes=ast_nodes,
        dependency_edges=dependency_edges,
        operations=len(trace.operations),
        estimated_persistence_bytes=(
            sum(item_counts.values()) * 96
            + addressable // 8
            + populated * 192
            + len(trace.operations) * 128
        ),
        virtual_events=len(trace.operations) * 2
        + int(metadata["workload"]["profile"].get("event_recalculations", 0)),
    )
    profile = WorkloadProfile.from_dict(metadata["workload"]["profile"])
    violations = metrics.budget_violations(profile.budgets)
    if violations:
        raise WorkloadBudgetError(violations)
    metadata["workload"]["metrics"] = metrics.to_dict()
    return replace(trace, metadata=metadata)


def _product(values: Iterable[int]) -> int:
    result = 1
    for value in values:
        result *= int(value)
    return result


def _ast_reference_count(value: dict[str, Any] | None) -> int:
    if value is None:
        return 0
    count = 0
    pending = [value]
    while pending:
        node = pending.pop()
        if node.get("type") == "reference":
            count += 1
        node_type = node.get("type")
        if node_type in {"binary", "comparison"}:
            pending.extend((node["left"], node["right"]))
        elif node_type == "negate":
            pending.append(node["operand"])
        elif node_type == "call":
            pending.extend(node["arguments"])
    return count
