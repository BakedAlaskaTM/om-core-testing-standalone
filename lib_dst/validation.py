"""Structural validation for replayable and shrinkable DST traces."""

from __future__ import annotations

from lib_dst.ast import Binary, Call, Comparison, Negate, Reference, from_dict, render
from lib_dst.faults import decisions_from_trace
from lib_dst.profiles import StructuralBudgets, WorkloadMetrics, WorkloadProfile
from lib_dst.schemas import TraceTranscript
from lib_dst.scheduling import scenario_from_trace
from lib_dst.symbols import SymbolKind
from lib_openm.rule_eval import _Parser, _tokenise


class TraceValidationError(ValueError):
    pass


def validate_trace(trace: TraceTranscript) -> None:
    try:
        _validate_trace(trace)
    except TraceValidationError:
        raise
    except (KeyError, TypeError, ValueError, SyntaxError) as exc:
        raise TraceValidationError(str(exc)) from exc


def _validate_trace(trace: TraceTranscript) -> None:
    symbols = trace.metadata.get("symbols", {})
    created: set[str] = set()
    dimension_names: set[str] = set()
    cube_names: set[str] = set()
    configured_serial = False
    rule_expressions: list[str] = []
    rule_steps: dict[int, str] = {}
    hardvalue_handles: set[str] = set()

    for operation in trace.operations:
        args = operation.args
        if operation.kind == "configure_serial":
            if args.get("enabled") is not False:
                raise TraceValidationError("DST traces must force serial recalculation")
            configured_serial = True
        elif operation.kind == "create_dimension":
            _require_symbol(symbols, operation.target, SymbolKind.DIMENSION)
            _require_new(operation.target, created)
            dimension_names.add(str(args["name"]))
            created.add(operation.target)
        elif operation.kind == "create_dimension_item":
            _require_symbol(symbols, operation.target, SymbolKind.ITEM)
            _require_created(str(args["dimension"]), created)
            _require_new(operation.target, created)
            created.add(operation.target)
        elif operation.kind == "create_cube":
            _require_symbol(symbols, operation.target, SymbolKind.CUBE)
            _require_new(operation.target, created)
            for handle in args["dimensions"]:
                _require_created(str(handle), created)
            cube_names.add(str(args["name"]))
            created.add(operation.target)
        elif operation.kind == "create_udf":
            _require_symbol(symbols, operation.target, SymbolKind.UDF)
            _require_new(operation.target, created)
            if not args.get("name") or not isinstance(args.get("params"), list):
                raise TraceValidationError("Invalid UDF definition")
            if not str(args.get("expression", "")).strip():
                raise TraceValidationError("UDF expression cannot be empty")
            created.add(operation.target)
        elif operation.kind == "set_dimension_outline":
            _require_created(operation.target, created)
            _validate_outline(args.get("outline"), created)
        elif operation.kind == "create_view":
            _require_symbol(symbols, operation.target, SymbolKind.VIEW)
            _require_new(operation.target, created)
            _require_created(str(args["cube"]), created)
            for axis in ("rows", "cols", "pages"):
                for handle in args.get(axis, ()):
                    _require_created(str(handle), created)
            created.add(operation.target)
        elif operation.kind == "set_cell_hardvalue":
            _require_created(operation.target, created)
            for handle in args["address"]:
                _require_created(str(handle), created)
                hardvalue_handles.add(str(handle))
        elif operation.kind == "set_cell_channel":
            _require_created(operation.target, created)
            if args.get("channel") not in {
                "bold",
                "italic",
                "underline",
                "color",
                "bg_color",
                "font_size",
                "number_format",
            }:
                raise TraceValidationError("Unsupported sampled format channel")
            for handle in args["address"]:
                _require_created(str(handle), created)
        elif operation.kind == "set_rule":
            _require_created(operation.target, created)
            expression = str(args["expression"])
            _Parser(_tokenise(expression)).parse()
            rule_expressions.append(expression)
            rule_steps[operation.step] = expression
        elif operation.kind == "run_recalculation":
            continue
        elif operation.kind == "delete_cube":
            _require_created(operation.target, created)
        else:
            raise TraceValidationError(f"Unsupported operation: {operation.kind}")

    if not configured_serial:
        raise TraceValidationError("Trace is missing serial recalculation configuration")
    if not set(trace.observe.dimensions).issubset(dimension_names):
        raise TraceValidationError("Observation references an uncreated dimension")
    if not set(trace.observe.cubes).issubset(cube_names):
        raise TraceValidationError("Observation references an uncreated cube")
    for cell in trace.observe.cells:
        if cell.cube not in trace.observe.cubes:
            raise TraceValidationError("Sampled cell references an unobserved cube")
        if not cell.address or not str(cell.address[0]).startswith("@."):
            raise TraceValidationError("Sampled cell must use a canonical @ channel address")

    ast_value = trace.metadata.get("ast")
    if ast_value is not None:
        expression = from_dict(ast_value)
        rendered = render(expression)
        if rendered not in rule_expressions:
            raise TraceValidationError("Serialized AST does not match a rule expression")
        missing_values = _references(expression) - hardvalue_handles
        if missing_values and trace.metadata.get("generator") != "phase45.scale.v1":
            raise TraceValidationError(
                f"AST references inputs without hard values: {sorted(missing_values)}"
            )

    oracle = trace.metadata.get("oracle", {})
    target = oracle.get("target", {})
    if target.get("cube") not in trace.observe.cubes:
        raise TraceValidationError("Oracle target cube is not observed")
    if "expected" not in oracle:
        raise TraceValidationError("Trace is missing its oracle expectation")
    sampled = {(cell.cube, cell.address) for cell in trace.observe.cells}
    for probe in ({"target": target, "expected": oracle.get("expected")}, *oracle.get("probes", ())):
        probe_target = probe.get("target", {})
        if probe_target.get("cube") not in trace.observe.cubes:
            raise TraceValidationError("Oracle probe cube is not observed")
        if "expected" not in probe:
            raise TraceValidationError("Oracle probe is missing its expectation")
        identity = (str(probe_target.get("cube")), tuple(probe_target.get("address", ())))
        if sampled and identity not in sampled:
            raise TraceValidationError("Oracle probe is not materialized in observation samples")

    rule_asts = trace.metadata.get("rule_asts")
    if rule_asts is not None:
        covered_steps: set[int] = set()
        for entry in rule_asts:
            step = int(entry["step"])
            if step not in rule_steps or rule_steps[step] != entry["expression"]:
                raise TraceValidationError("Recorded rule AST does not match its operation")
            covered_steps.add(step)
            value = entry.get("ast")
            if value is not None and render(from_dict(value)) != entry["expression"]:
                raise TraceValidationError("Recorded rule AST does not render to its expression")
            if "expected" not in entry or "target" not in entry:
                raise TraceValidationError("Recorded rule is missing target or expectation")
        if covered_steps != set(rule_steps):
            raise TraceValidationError("Every scale rule must have recorded oracle metadata")

    workload = trace.metadata.get("workload")
    if workload is not None:
        profile = WorkloadProfile.from_dict(workload["profile"])
        metrics = WorkloadMetrics.from_dict(workload["metrics"])
        violations = metrics.budget_violations(profile.budgets)
        if violations:
            raise TraceValidationError(
                "Recorded workload exceeds budget: " + ", ".join(violations)
            )
        if metrics.operations != len(trace.operations):
            raise TraceValidationError("Recorded operation count does not match trace")
        if metrics.rules != len(rule_steps):
            raise TraceValidationError("Recorded rule count does not match trace")

    decisions = decisions_from_trace(trace)
    identities = [(decision.operation, decision.occurrence) for decision in decisions]
    if len(identities) != len(set(identities)):
        raise TraceValidationError("Duplicate fault decision occurrence")

    scenario_from_trace(trace)

    # Imported lazily because mutation application itself validates candidates.
    from lib_dst.mutation import mutation_decisions_from_trace

    mutation_decisions_from_trace(trace)


def _validate_outline(value, created: set[str]) -> None:
    if not isinstance(value, list):
        raise TraceValidationError("Dimension outline must be a list")
    for node in value:
        if not isinstance(node, dict) or not str(node.get("label", "")):
            raise TraceValidationError("Invalid dimension outline node")
        item = node.get("item")
        if item is not None:
            _require_created(str(item), created)
        _validate_outline(node.get("children", []), created)


def _require_symbol(symbols: dict, handle: str, kind: SymbolKind) -> None:
    value = symbols.get(handle)
    if value is None or value.get("kind") != kind.value:
        raise TraceValidationError(f"Missing {kind.value} symbol: {handle}")


def _require_created(handle: str, created: set[str]) -> None:
    if handle not in created:
        raise TraceValidationError(f"Symbol used before creation: {handle}")


def _require_new(handle: str, created: set[str]) -> None:
    if handle in created:
        raise TraceValidationError(f"Symbol created twice: {handle}")


def _references(expression) -> set[str]:
    if isinstance(expression, Reference):
        return {expression.handle}
    if isinstance(expression, Negate):
        return _references(expression.operand)
    if isinstance(expression, (Binary, Comparison)):
        return _references(expression.left) | _references(expression.right)
    if isinstance(expression, Call):
        result: set[str] = set()
        for argument in expression.arguments:
            result.update(_references(argument))
        return result
    return set()
