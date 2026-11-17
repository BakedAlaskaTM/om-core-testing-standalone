"""Recorded, type-preserving mutations for deterministic corpus evolution."""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, replace
from typing import Any, Iterable

from lib_dst.ast import Binary, from_dict, render, to_dict
from lib_dst.faults import FaultDecision
from lib_dst.oracle import canonical, evaluate
from lib_dst.scale_generator import refresh_workload_metadata
from lib_dst.schemas import ObservationCell, TraceOperation, TraceTranscript
from lib_dst.scheduling import QUERY_TIMEOUT_NS, SchedulerScenario
from lib_dst.seeding import SeedStreams
from lib_dst.validation import validate_trace
from lib_openm.rule_eval import _Parser, _tokenise


MUTATION_VERSION = 1
_KINDS = {
    "hardvalue",
    "ast_literal",
    "ast_operator",
    "ast_reference",
    "recalculation",
    "scheduler_delay",
    "fault",
    "scale_cardinality",
    "scale_density",
    "scale_rule",
    "scale_graph",
    "operation_burst",
}


@dataclass(frozen=True)
class MutationDecision:
    parent_sha256: str
    mutation_seed: str
    mutation_index: int
    kind: str
    target: dict[str, Any]
    before: Any
    after: Any
    version: int = MUTATION_VERSION

    def __post_init__(self) -> None:
        if self.version != MUTATION_VERSION:
            raise ValueError(f"Unsupported mutation version: {self.version}")
        if len(self.parent_sha256) != 64:
            raise ValueError("Mutation parent must be a SHA-256 digest")
        int(self.parent_sha256, 16)
        int(self.mutation_seed, 16)
        if self.mutation_index < 0:
            raise ValueError("Mutation index cannot be negative")
        if self.kind not in _KINDS:
            raise ValueError(f"Unsupported mutation kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parent_sha256": self.parent_sha256,
            "mutation_seed": self.mutation_seed,
            "mutation_index": self.mutation_index,
            "kind": self.kind,
            "target": self.target,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MutationDecision":
        return cls(
            version=int(value.get("version", MUTATION_VERSION)),
            parent_sha256=str(value["parent_sha256"]),
            mutation_seed=str(value["mutation_seed"]),
            mutation_index=int(value["mutation_index"]),
            kind=str(value["kind"]),
            target=dict(value["target"]),
            before=value.get("before"),
            after=value.get("after"),
        )


def mutation_decisions_from_trace(
    trace: TraceTranscript,
) -> tuple[MutationDecision, ...]:
    decisions = tuple(
        MutationDecision.from_dict(value)
        for value in trace.metadata.get("mutations", ())
    )
    indices = [decision.mutation_index for decision in decisions]
    if len(indices) != len(set(indices)):
        raise ValueError("Duplicate mutation index")
    return decisions


def available_mutation_kinds(trace: TraceTranscript) -> tuple[str, ...]:
    if isinstance(trace.metadata.get("workload"), dict):
        kinds = ["scale_cardinality", "operation_burst"]
        unused = _scale_unused_addresses(trace)
        if unused:
            kinds.extend(("scale_density", "scale_rule"))
        if any(len(dimensions) == 1 for _, dimensions, _ in unused):
            kinds.append("scale_graph")
        if trace.metadata.get("faults"):
            kinds.append("fault")
        return tuple(sorted(kinds))
    ast_value = trace.metadata.get("ast", {})
    nodes = tuple(_walk_ast(ast_value)) if isinstance(ast_value, dict) else ()
    kinds: list[str] = []
    if any(operation.kind == "set_cell_hardvalue" for operation in trace.operations):
        kinds.append("hardvalue")
    if any(node.get("type") == "number" for _, node in nodes):
        kinds.append("ast_literal")
    if any(node.get("type") == "binary" for _, node in nodes):
        kinds.append("ast_operator")
    references = [node for _, node in nodes if node.get("type") == "reference"]
    hardvalue_handles = {
        str(handle)
        for operation in trace.operations
        if operation.kind == "set_cell_hardvalue"
        for handle in operation.args.get("address", ())
    }
    if references and any(
        hardvalue_handles - {str(node.get("handle"))} for node in references
    ):
        kinds.append("ast_reference")
    kinds.append("recalculation")
    scheduler = trace.metadata.get("scheduler")
    if isinstance(scheduler, dict) and scheduler.get("decisions"):
        kinds.append("scheduler_delay")
    if trace.metadata.get("faults"):
        kinds.append("fault")
    return tuple(sorted(kinds))


def mutate_trace(
    trace: TraceTranscript,
    master_seed: int | str,
    mutation_index: int,
    *,
    kind: str | None = None,
) -> tuple[TraceTranscript, MutationDecision]:
    """Create and record one deterministic mutation."""

    validate_trace(trace)
    streams = SeedStreams(master_seed)
    domain = f"mutation.{mutation_index}"
    seed = streams.seed(domain)
    rng = streams.random(domain)
    available = available_mutation_kinds(trace)
    selected = kind or rng.choice(available)
    if selected not in available:
        raise ValueError(f"Mutation kind {selected!r} is unavailable for this trace")
    decision = _draw_decision(trace, selected, mutation_index, seed, rng)
    return apply_mutation_decision(trace, decision), decision


def apply_mutation_decision(
    trace: TraceTranscript, decision: MutationDecision
) -> TraceTranscript:
    """Apply a materialized mutation without consulting a PRNG."""

    if trace.sha256() != decision.parent_sha256:
        raise ValueError("Mutation decision parent does not match the supplied trace")

    operations = list(trace.operations)
    metadata = copy.deepcopy(trace.metadata)
    ast_changed = False
    scale_candidate: TraceTranscript | None = None

    if decision.kind in {
        "scale_cardinality",
        "scale_density",
        "scale_rule",
        "scale_graph",
        "operation_burst",
    }:
        scale_candidate = _apply_scale_mutation(trace, decision)
        operations = list(scale_candidate.operations)
        metadata = copy.deepcopy(scale_candidate.metadata)
    elif decision.kind == "hardvalue":
        step = int(decision.target["step"])
        operation = operations[step]
        args = dict(operation.args)
        if args.get("value") != decision.before:
            raise ValueError("Hard-value mutation precondition changed")
        args["value"] = decision.after
        operations[step] = replace(operation, args=args)
    elif decision.kind in {"ast_literal", "ast_operator", "ast_reference"}:
        ast_value = copy.deepcopy(metadata["ast"])
        node = _node_at(ast_value, tuple(decision.target["path"]))
        field = str(decision.target["field"])
        if field == "__node__":
            if node != decision.before:
                raise ValueError("AST mutation precondition changed")
            node.clear()
            node.update(copy.deepcopy(decision.after))
        else:
            if node.get(field) != decision.before:
                raise ValueError("AST mutation precondition changed")
            node[field] = copy.deepcopy(decision.after)
        expression = render(from_dict(ast_value))
        _Parser(_tokenise(expression)).parse()
        metadata["ast"] = ast_value
        for index, operation in enumerate(operations):
            if operation.kind == "set_rule":
                args = dict(operation.args)
                args["expression"] = expression
                operations[index] = replace(operation, args=args)
        ast_changed = True
    elif decision.kind == "recalculation":
        action = str(decision.target["action"])
        if action == "delete":
            step = int(decision.target["step"])
            if operations[step].kind != "run_recalculation":
                raise ValueError("Recalculation deletion precondition changed")
            del operations[step]
        elif action == "insert":
            operations.append(
                TraceOperation(len(operations), "run_recalculation", "runtime", {"scope": "all"})
            )
        else:
            raise ValueError(f"Unsupported recalculation action: {action}")
        operations = [replace(operation, step=index) for index, operation in enumerate(operations)]
    elif decision.kind == "scheduler_delay":
        scheduler = copy.deepcopy(metadata["scheduler"])
        index = int(decision.target["decision_index"])
        if scheduler["decisions"][index]["delay_ns"] != decision.before:
            raise ValueError("Scheduler mutation precondition changed")
        scheduler["decisions"][index]["delay_ns"] = int(decision.after)
        SchedulerScenario.from_dict(scheduler)
        metadata["scheduler"] = scheduler
    elif decision.kind == "fault":
        faults = copy.deepcopy(metadata["faults"])
        index = int(decision.target["decision_index"])
        field = str(decision.target["field"])
        if faults[index][field] != decision.before:
            raise ValueError("Fault mutation precondition changed")
        faults[index][field] = decision.after
        FaultDecision.from_dict(faults[index])
        metadata["faults"] = faults
    else:  # pragma: no cover - guarded by MutationDecision
        raise ValueError(f"Unsupported mutation kind: {decision.kind}")

    candidate = scale_candidate or replace(
        trace, operations=tuple(operations), metadata=metadata
    )
    if decision.kind == "hardvalue" or ast_changed:
        candidate = _refresh_oracle(candidate)

    metadata = copy.deepcopy(candidate.metadata)
    recorded = list(metadata.get("mutations", ()))
    recorded.append(decision.to_dict())
    metadata["mutations"] = recorded
    features = set(str(value) for value in metadata.get("features", ()))
    features.add(f"mutation:{decision.kind}")
    metadata["features"] = sorted(features)
    subseeds = dict(metadata.get("subseeds", {}))
    subseeds[f"mutation.{decision.mutation_index}"] = decision.mutation_seed
    metadata["subseeds"] = subseeds
    candidate = replace(candidate, metadata=metadata)
    validate_trace(candidate)
    return candidate


def _draw_decision(trace, kind, mutation_index, seed, rng) -> MutationDecision:
    target: dict[str, Any]
    before: Any
    after: Any
    if kind in {
        "scale_cardinality",
        "scale_density",
        "scale_rule",
        "scale_graph",
        "operation_burst",
    }:
        target, before, after = _draw_scale_decision(
            trace, kind, mutation_index, rng
        )
    elif kind == "hardvalue":
        candidates = [operation for operation in trace.operations if operation.kind == "set_cell_hardvalue"]
        operation = rng.choice(candidates)
        target = {"step": operation.step}
        before = operation.args["value"]
        after = _different(rng, before, (-1, 0, 1, int(before) - 1, int(before) + 1))
    elif kind in {"ast_literal", "ast_operator", "ast_reference"}:
        ast_value = trace.metadata["ast"]
        node_type = {
            "ast_literal": "number",
            "ast_operator": "binary",
            "ast_reference": "reference",
        }[kind]
        candidates = [(path, node) for path, node in _walk_ast(ast_value) if node.get("type") == node_type]
        path, node = rng.choice(candidates)
        if kind == "ast_literal":
            field = "value"
            before = node[field]
            after = _different(rng, before, (-1, 0, 1, int(before) - 1, int(before) + 1))
        elif kind == "ast_operator":
            field = "operator"
            before = node[field]
            after = rng.choice(sorted({"+", "-", "*", "/"} - {str(before)}))
        else:
            field = "__node__"
            before = copy.deepcopy(node)
            hardvalue_handles = sorted(
                {
                    str(handle)
                    for operation in trace.operations
                    if operation.kind == "set_cell_hardvalue"
                    for handle in operation.args.get("address", ())
                    if str(handle) != str(node.get("handle"))
                }
            )
            after = _reference_node(trace, rng.choice(hardvalue_handles))
        target = {"path": list(path), "field": field}
    elif kind == "recalculation":
        recalculations = [operation for operation in trace.operations if operation.kind == "run_recalculation"]
        if recalculations and rng.randrange(2) == 0:
            operation = rng.choice(recalculations)
            target = {"action": "delete", "step": operation.step}
            before, after = operation.to_dict(), None
        else:
            target = {"action": "insert", "step": len(trace.operations)}
            before, after = None, {"kind": "run_recalculation", "scope": "all"}
    elif kind == "scheduler_delay":
        scheduler = trace.metadata["scheduler"]
        index = rng.randrange(len(scheduler["decisions"]))
        item = scheduler["decisions"][index]
        before = int(item["delay_ns"])
        if item["mode"] == "timeout":
            after = QUERY_TIMEOUT_NS + rng.randint(1, 10_000)
        else:
            after = _different(rng, before, (0, 1, 10, 100, 1_000, 10_000))
        target = {"decision_index": index}
    elif kind == "fault":
        faults = trace.metadata["faults"]
        index = rng.randrange(len(faults))
        item = faults[index]
        if item["operation"] == "load":
            target = {"decision_index": index, "field": "outcome"}
            before = item["outcome"]
            after = "malformed_json" if before == "raise" else "raise"
        else:
            target = {"decision_index": index, "field": "occurrence"}
            before = int(item["occurrence"])
            after = 2 if before == 1 else 1
    else:  # pragma: no cover - selected from available kinds
        raise ValueError(kind)
    return MutationDecision(
        parent_sha256=trace.sha256(),
        mutation_seed=f"{seed:032x}",
        mutation_index=mutation_index,
        kind=kind,
        target=target,
        before=before,
        after=after,
    )


def _different(rng, before: Any, values: Iterable[Any]) -> Any:
    candidates = sorted(set(values) - {before}, key=lambda value: (type(value).__name__, repr(value)))
    if not candidates:
        raise ValueError("Mutation has no alternative value")
    return rng.choice(candidates)


def _walk_ast(value: dict[str, Any], path: tuple[Any, ...] = ()):
    yield path, value
    node_type = value.get("type")
    if node_type in {"binary", "comparison"}:
        yield from _walk_ast(value["left"], path + ("left",))
        yield from _walk_ast(value["right"], path + ("right",))
    elif node_type == "negate":
        yield from _walk_ast(value["operand"], path + ("operand",))
    elif node_type == "call":
        for index, argument in enumerate(value["arguments"]):
            yield from _walk_ast(argument, path + ("arguments", index))


def _node_at(value: dict[str, Any], path: tuple[Any, ...]) -> dict[str, Any]:
    current: Any = value
    for part in path:
        current = current[part]
    if not isinstance(current, dict):
        raise ValueError("Mutation path does not identify an AST node")
    return current


def _refresh_oracle(trace: TraceTranscript) -> TraceTranscript:
    expression = from_dict(trace.metadata["ast"])
    values: dict[str, Any] = {}
    for operation in trace.operations:
        if operation.kind == "set_cell_hardvalue":
            address = operation.args.get("address", ())
            if len(address) == 1:
                values[str(address[0])] = operation.args["value"]
    metadata = copy.deepcopy(trace.metadata)
    oracle = dict(metadata["oracle"])
    oracle["expected"] = canonical(evaluate(expression, values))
    metadata["oracle"] = oracle
    return replace(trace, metadata=metadata)


def _reference_node(trace: TraceTranscript, handle: str) -> dict[str, Any]:
    symbols = trace.metadata["symbols"]
    item = symbols[handle]
    dimension = symbols[item["owner"]]
    cube = next(
        operation.args["name"]
        for operation in trace.operations
        if operation.kind == "create_cube"
    )
    return {
        "type": "reference",
        "handle": handle,
        "cube": cube,
        "dimension": dimension["name"],
        "item": item["name"],
    }


def _draw_scale_decision(trace, kind, mutation_index, rng):
    symbols = trace.metadata["symbols"]
    if kind == "scale_cardinality":
        dimensions = sorted(
            handle
            for handle, value in symbols.items()
            if value.get("kind") == "dimension"
        )
        dimension = rng.choice(dimensions)
        existing = sum(
            value.get("kind") == "item" and value.get("owner") == dimension
            for value in symbols.values()
        )
        target = {"dimension": dimension}
        before = existing
        after = {
            "handle": f"item:mutation/{mutation_index}/{existing}",
            "name": f"MutItem{mutation_index:04d}_{existing:04d}",
        }
        return target, before, after
    if kind == "operation_burst":
        count = rng.randint(1, 4)
        return {"insert": "tail"}, 0, count

    candidates = _scale_unused_addresses(trace)
    if kind == "scale_graph":
        candidates = [candidate for candidate in candidates if len(candidate[1]) == 1]
    cube, dimensions, address = rng.choice(candidates)
    target = {"cube": cube, "address": list(address)}
    before = None
    if kind == "scale_density":
        after = {"value": rng.randint(-20, 20)}
    elif kind == "scale_rule":
        value = rng.randint(-20, 20)
        after = {
            "expression": str(value),
            "ast": {"type": "number", "value": value},
            "expected": {"number": float(value)},
        }
    else:
        cube_name = symbols[cube]["name"]
        probe = next(
            probe
            for probe in _all_oracle_probes(trace)
            if probe["target"]["cube"] == cube_name
            and "number" in probe["expected"]
        )
        source_name = probe["target"]["address"][1]
        dimension_handle = dimensions[0]
        source_handle = next(
            handle
            for handle, value in symbols.items()
            if value.get("kind") == "item"
            and value.get("owner") == dimension_handle
            and value.get("name") == source_name
        )
        expression = Binary(
            "+",
            from_dict(
                {
                    "type": "reference",
                    "handle": source_handle,
                    "cube": cube_name,
                    "dimension": symbols[dimension_handle]["name"],
                    "item": source_name,
                }
            ),
            from_dict({"type": "number", "value": 1}),
        )
        after = {
            "expression": render(expression),
            "ast": to_dict(expression),
            "expected": {"number": float(probe["expected"]["number"]) + 1.0},
        }
    return target, before, after


def _apply_scale_mutation(
    trace: TraceTranscript, decision: MutationDecision
) -> TraceTranscript:
    operations = list(trace.operations)
    metadata = copy.deepcopy(trace.metadata)
    observe = trace.observe
    if decision.kind == "scale_cardinality":
        dimension = str(decision.target["dimension"])
        existing = sum(
            value.get("kind") == "item" and value.get("owner") == dimension
            for value in metadata["symbols"].values()
        )
        if existing != decision.before:
            raise ValueError("Scale-cardinality mutation precondition changed")
        handle = str(decision.after["handle"])
        name = str(decision.after["name"])
        if handle in metadata["symbols"]:
            raise ValueError("Scale-cardinality handle already exists")
        metadata["symbols"][handle] = {
            "kind": "item",
            "name": name,
            "owner": dimension,
        }
        insertion = max(
            index
            for index, operation in enumerate(operations)
            if operation.kind == "create_dimension_item"
            and operation.args["dimension"] == dimension
        ) + 1
        operations.insert(
            insertion,
            TraceOperation(-1, "create_dimension_item", handle, {"dimension": dimension, "name": name}),
        )
    elif decision.kind == "operation_burst":
        for _ in range(int(decision.after)):
            operations.append(TraceOperation(-1, "run_recalculation", "runtime", {"scope": "all"}))
    else:
        cube = str(decision.target["cube"])
        address = tuple(str(handle) for handle in decision.target["address"])
        if not any(
            candidate_cube == cube and candidate_address == address
            for candidate_cube, _, candidate_address in _scale_unused_addresses(trace)
        ):
            raise ValueError("Scale mutation target is no longer unused")
        insertion = next(
            (
                index
                for index, operation in enumerate(operations)
                if operation.kind == "run_recalculation"
            ),
            len(operations),
        )
        if decision.kind == "scale_density":
            operations.insert(
                insertion,
                TraceOperation(
                    -1,
                    "set_cell_hardvalue",
                    cube,
                    {"address": list(address), "value": decision.after["value"]},
                ),
            )
        else:
            symbols = metadata["symbols"]
            targets = [
                [symbols[symbols[handle]["owner"]]["name"], symbols[handle]["name"]]
                for handle in address
            ]
            operations.insert(
                insertion,
                TraceOperation(
                    -1,
                    "set_rule",
                    cube,
                    {
                        "targets": targets,
                        "expression": decision.after["expression"],
                        "is_anchored": True,
                    },
                ),
            )
            cube_name = symbols[cube]["name"]
            target = {
                "cube": cube_name,
                "address": ["@.value", *(symbols[handle]["name"] for handle in address)],
            }
            metadata["oracle"].setdefault("probes", []).append(
                {"target": target, "expected": copy.deepcopy(decision.after["expected"])}
            )
            metadata.setdefault("rule_asts", []).append(
                {
                    "step": -1,
                    "cube": cube_name,
                    "target": target,
                    "expression": decision.after["expression"],
                    "ast": copy.deepcopy(decision.after["ast"]),
                    "expected": copy.deepcopy(decision.after["expected"]),
                }
            )
        cube_name = metadata["symbols"][cube]["name"]
        logical = ObservationCell(
            cube_name,
            ("@.value", *(metadata["symbols"][handle]["name"] for handle in address)),
        )
        observe = replace(
            observe,
            cells=tuple(sorted({*observe.cells, logical}, key=lambda cell: (cell.cube, cell.address))),
        )

    operations = [replace(operation, step=index) for index, operation in enumerate(operations)]
    if metadata.get("rule_asts") is not None:
        rule_entries = list(metadata["rule_asts"])
        rule_operations = [operation for operation in operations if operation.kind == "set_rule"]
        if len(rule_entries) != len(rule_operations):
            raise ValueError("Scale mutation rule metadata is incomplete")
        for entry, operation in zip(rule_entries, rule_operations):
            entry["step"] = operation.step
    candidate = replace(trace, operations=tuple(operations), observe=observe, metadata=metadata)
    return refresh_workload_metadata(candidate)


def _scale_unused_addresses(
    trace: TraceTranscript,
) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    symbols = trace.metadata.get("symbols", {})
    items_by_dimension: dict[str, list[str]] = {}
    for handle, value in symbols.items():
        if value.get("kind") == "item":
            items_by_dimension.setdefault(str(value["owner"]), []).append(handle)
    for values in items_by_dimension.values():
        values.sort()
    used: dict[str, set[tuple[str, ...]]] = {}
    name_lookup = {
        (str(value.get("owner")), str(value.get("name"))): handle
        for handle, value in symbols.items()
        if value.get("kind") == "item"
    }
    cube_dimensions: dict[str, tuple[str, ...]] = {}
    for operation in trace.operations:
        if operation.kind == "create_cube":
            cube_dimensions[operation.target] = tuple(operation.args["dimensions"])
        elif operation.kind == "set_cell_hardvalue":
            used.setdefault(operation.target, set()).add(tuple(operation.args["address"]))
        elif operation.kind == "set_rule":
            dimensions = cube_dimensions[operation.target]
            address = tuple(
                name_lookup[(dimension, target[1])]
                for dimension, target in zip(dimensions, operation.args["targets"])
            )
            used.setdefault(operation.target, set()).add(address)
    result: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for cube, dimensions in sorted(cube_dimensions.items()):
        for address in itertools.product(*(items_by_dimension[dimension] for dimension in dimensions)):
            if tuple(address) not in used.get(cube, set()):
                result.append((cube, dimensions, tuple(address)))
                break
    return result


def _all_oracle_probes(trace: TraceTranscript) -> list[dict[str, Any]]:
    oracle = trace.metadata["oracle"]
    return [
        {"target": oracle["target"], "expected": oracle["expected"]},
        *list(oracle.get("probes", ())),
    ]
