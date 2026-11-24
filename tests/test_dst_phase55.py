"""Phase 5.5 step 1: direct tests of dynamic range bounds, ``$<...>``.

Every model is hand-built and driven through the public ``CommandSession``. Three checks
need no contract decisions (plan-v2.md, section 4.1):

1. substitution: ``$<cell>`` equals the same expression with the cell's value written as
   a literal (and both equal an expected value computed here, independently);
2. recalculation: an incremental read equals the value after a full recalculation, and
   repeating the recalculation changes nothing;
3. cold reload: values survive save/load into a fresh engine, which still reacts to edits.

Known violations of those checks are strict ``xfail`` findings: a repair makes them fail
loudly. Behaviour awaiting an answer from the OM Core creator (D1-D10) is *recorded, not
asserted*: ``test_awaiting_contract`` only warns when the observed result changes.
"""

from __future__ import annotations

import contextlib
import io
import math
import warnings
from typing import Any, Callable, Iterator, Sequence

import pytest

from lib_dst.generator import generate_trace
from lib_dst.executor import create_trace_runtime
from lib_dst.seeding import SeedStreams
from lib_runtime.lifecycle import reset_runtime_state
from lib_storeadapters.memory_adapters import InMemoryJsonAdapter

YEARS = ("2001", "2002", "2003", "2004", "2005")
MONTHS = ("Jan", "Feb", "Mar", "Apr")
REGIONS = ("N", "S")
PATH = "memory://dst/phase55.openm"
BOTH = "Data::[Year.$<Inputs::P.Start>..$<Inputs::P.End>]"
RESULT = (("Line", "R1"),)

# Reducers over the populated values in a range, written independently of the engine.
REDUCERS: dict[str, Callable[[list[float]], float]] = {
    "SUM": sum,
    "AVG": lambda values: sum(values) / len(values),
    "AVERAGE": lambda values: sum(values) / len(values),
    "MIN": min,
    "MAX": max,
    "COUNT": len,
    "COUNTA": len,
}

Key = tuple[str, str]


class NotSettled(AssertionError):
    """Check 2 failed: the value depends on recalculation."""


class NotSubstitutable(AssertionError):
    """Check 1 failed: a dynamic bound differs from its literal value."""


class Model:
    """A small workspace addressed by names and driven by public commands only."""

    def __init__(self) -> None:
        self.adapter = InMemoryJsonAdapter()
        self.runtime: Any = None
        self.dims: dict[str, str] = {}
        self.items: dict[Key, str] = {}
        self.cubes: dict[str, str] = {}
        self._start(None)

    def _start(self, workspace: Any) -> None:
        self.runtime = create_trace_runtime(
            generate_trace(0), workspace=workspace, workspace_adapter=self.adapter
        )
        self.run("set_multithread_recompute", enabled=False)

    def close(self) -> None:
        if self.runtime is not None:
            reset_runtime_state(self.runtime)
            self.runtime = None

    def run(self, command: str, **params: Any) -> Any:
        result = self.runtime.session.execute(command, **params)
        assert result.success, f"{command}: {result.error}"
        return result.data

    def dimension(self, name: str, dim_type: str, items: Sequence[str]) -> None:
        self.dims[name] = self.run("create_dimension", name=name, dim_type=dim_type)["id"]
        for item in items:
            self.items[name, item] = self.run(
                "create_dimension_item", dim_id=self.dims[name], name=item
            )["id"]

    def cube(self, name: str, dims: Sequence[str]) -> None:
        self.cubes[name] = self.run(
            "create_cube", name=name, dimension_ids=[self.dims[dim] for dim in dims]
        )["id"]

    def _address(self, cube: str, keys: Sequence[Key]) -> list[str]:
        workspace = self.runtime.workspace
        prefix = []
        if workspace.cubes[self.cubes[cube]].dimension_ids[0] == "@":
            prefix = [workspace.dimensions["@"].items[0].id]
        return prefix + [self.items[key] for key in keys]

    def set(self, cube: str, keys: Sequence[Key], value: Any) -> None:
        self.run(
            "set_cell_hardvalue_by_addr",
            cube_id=self.cubes[cube],
            addr=self._address(cube, keys),
            value=value,
        )

    def control(self, name: str, value: Any) -> None:
        self.set("Inputs", [("P", name)], value)

    def read(self, cube: str = "Out", keys: Sequence[Key] = RESULT) -> Any:
        return self.runtime.engine.get_cell_by_addr(
            self.runtime.workspace.cubes[self.cubes[cube]], tuple(self._address(cube, keys))
        )

    def try_rule(
        self,
        expression: str,
        cube: str = "Out",
        targets: Sequence[Key] = RESULT,
        anchored: bool = True,
    ) -> str | None:
        """Set a rule and return the rejection message, or ``None`` on success."""

        result = self.runtime.session.execute(
            "set_rule",
            cube_id=self.cubes[cube],
            targets=list(targets),
            expression=expression,
            is_anchored=anchored,
        )
        return None if result.success else str(result.error)

    def rule(self, expression: str, **kwargs: Any) -> None:
        error = self.try_rule(expression, **kwargs)
        assert error is None, f"{expression}: {error}"

    def recalculate(self) -> None:
        self.run("run_recalculation", scope="all")

    def rename(self, dim: str, item: str, new_name: str) -> None:
        self.run(
            "rename_dimension_item",
            dim_id=self.dims[dim],
            item_id=self.items[dim, item],
            new_name=new_name,
        )

    def reload(self) -> None:
        """Save, tear the runtime down, and reload into a genuinely fresh engine."""

        self.run("save_workspace", path=PATH)
        self.close()
        self._start(self.adapter.load_workspace(PATH))
        self.runtime.engine.bootstrap_dependency_graph()
        self.recalculate()


def build() -> Model:
    model = Model()
    try:
        model.dimension("Year", "seq", YEARS)
        model.dimension("Month", "seq", MONTHS)
        model.dimension("Kind", "set", ("A", "B", "C"))
        model.dimension("Region", "set", REGIONS)
        model.dimension("P", "set", ("Start", "End", "Ctl", "Zero"))
        model.dimension("Line", "set", ("R1", "R2"))
        model.cube("Data", ["Year"])
        model.cube("Sparse", ["Year"])
        model.cube("MData", ["Month"])
        model.cube("KData", ["Kind"])
        model.cube("Two", ["Year", "Region"])
        model.cube("Inputs", ["P"])
        model.cube("Out", ["Line"])
        model.cube("OutR", ["Region"])
        for index, year in enumerate(YEARS, start=1):
            model.set("Data", [("Year", year)], 10 * index)
            model.set("Two", [("Year", year), ("Region", "N")], index)
            model.set("Two", [("Year", year), ("Region", "S")], 100 * index)
        for year in ("2001", "2003"):
            model.set("Sparse", [("Year", year)], int(year) - 2000)
        for index, month in enumerate(MONTHS, start=1):
            model.set("MData", [("Month", month)], index)
        for index, kind in enumerate("ABC", start=1):
            model.set("KData", [("Kind", kind)], index)
        model.control("Start", 2002)
        model.control("End", 2004)
        model.control("Zero", 0)
    except BaseException:
        model.close()
        raise
    return model


@pytest.fixture
def model() -> Iterator[Model]:
    with contextlib.redirect_stdout(io.StringIO()):
        value = build()
        try:
            yield value
        finally:
            value.close()


def same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
    return type(left) is type(right) and (left == right or repr(left) == repr(right))


def assert_settled(
    model: Model, cube: str = "Out", keys: Sequence[Key] = RESULT, rounds: int = 3
) -> Any:
    """Check 2: the incremental read survives repeated full recalculation."""

    first = model.read(cube, keys)
    for number in range(1, rounds + 1):
        model.recalculate()
        after = model.read(cube, keys)
        if not same(first, after):
            raise NotSettled(
                f"read {first!r} but full recalculation {number} gave {after!r}"
            )
    return first


def span(first: int, last: int) -> list[float]:
    return [10.0 * (year - 2000) for year in range(min(first, last), max(first, last) + 1)]


# ----------------------------------------------------------- substitution and value


@pytest.mark.parametrize("function", sorted(REDUCERS))
@pytest.mark.parametrize(
    ("dynamic", "literal", "first", "last"),
    [
        ("Data::Year.2001..$<Inputs::P.End>", "Data::Year.2001..2004", 2001, 2004),
        ("Data::[Year.2001..$<Inputs::P.End>]", "Data::[Year.2001..2004]", 2001, 2004),
        ("Data::[Year.$<Inputs::P.Start>..2005]", "Data::[Year.2002..2005]", 2002, 2005),
        (BOTH, "Data::[Year.2002..2004]", 2002, 2004),
        ("Data::[Year.$<Inputs::P.End>..$<Inputs::P.End>]", "Data::[Year.2004..2004]", 2004, 2004),
    ],
    ids=["end", "end-bracketed", "start-bracketed", "both-bracketed", "single-item"],
)
def test_bound_positions(
    model: Model, function: str, dynamic: str, literal: str, first: int, last: int
) -> None:
    expected = REDUCERS[function](span(first, last))
    model.rule(f"{function}({dynamic})")
    model.rule(f"{function}({literal})", targets=[("Line", "R2")])
    assert same(model.read("Out", [("Line", "R2")]), expected)
    assert same(assert_settled(model), expected)


@pytest.mark.parametrize(
    "value", [2004, 2004.0, "2004", " 2004 "], ids=["int", "float", "text", "spaced-text"]
)
def test_control_value_types_match_the_same_item(model: Model, value: Any) -> None:
    model.control("End", value)
    model.rule(f"SUM({BOTH})")
    assert same(assert_settled(model), 90.0)


def test_text_items_are_matched(model: Model) -> None:
    model.control("Ctl", "Mar")
    model.rule("SUM(MData::[Month.Feb..$<Inputs::P.Ctl>])")
    assert same(assert_settled(model), 5.0)
    model.control("Ctl", "Feb")
    assert same(assert_settled(model), 2.0)


def test_rule_computed_control_propagates(model: Model) -> None:
    model.rule("Inputs::P.End - 1", cube="Inputs", targets=[("P", "Ctl")])
    model.rule("SUM(Data::[Year.2001..$<Inputs::P.Ctl>])")
    assert same(assert_settled(model), 60.0)
    model.control("End", 2005)
    assert same(assert_settled(model), 100.0)


@pytest.mark.parametrize(
    ("inner", "last"), [("2003", 2003), ("Inputs::P.End - 1", 2003), ("Inputs::P.Start + 1", 2003)]
)
def test_literals_and_arithmetic_inside_a_bound(model: Model, inner: str, last: int) -> None:
    model.rule(f"SUM(Data::[Year.2001..$<{inner}>])")
    assert same(assert_settled(model), sum(span(2001, last)))


@pytest.mark.parametrize("function", ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTA"])
def test_sparse_range_matches_literal_range(model: Model, function: str) -> None:
    """No expected value is asserted: how empty cells count is not a bounds question."""

    model.rule(f"{function}(Sparse::[Year.2001..$<Inputs::P.End>])")
    model.rule(f"{function}(Sparse::[Year.2001..2004])", targets=[("Line", "R2")])
    assert same(assert_settled(model), model.read("Out", [("Line", "R2")]))


@pytest.mark.parametrize("function", sorted(REDUCERS))
def test_several_cross_cube_arguments(model: Model, function: str) -> None:
    """Several cross-cube arguments take the separate ``slice_over_ref`` path."""

    data = {2001: 10.0, 2002: 20.0, 2003: 30.0, 2004: 40.0, 2005: 50.0}
    model.rule(f"{function}({BOTH}, Data::Year.2001)")

    def expected(last: int) -> float:
        return REDUCERS[function]([data[year] for year in range(2002, last + 1)] + [data[2001]])

    assert same(assert_settled(model), expected(2004))
    model.control("End", 2005)
    assert same(model.read(), expected(2005))
    for year, value in ((2005, 1000.0), (2002, 0.0)):
        data[year] = value
        model.set("Data", [("Year", str(year))], value)
        assert same(model.read(), expected(2005))
    assert same(assert_settled(model), expected(2005))
    model.reload()
    assert same(model.read(), expected(2005))


SAME_CUBE = {"cube": "Data", "targets": [("Year", "2005")]}


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(Data::[Year.2002..$<Inputs::P.End>])",
        "SUM([Year.2002..$<2004>])",
        "SUM(SLICE(Data::[Year.2002..$<Inputs::P.End>]))",
        "SUM(SLICE([Year.2002..$<2004>]))",
    ],
)
def test_same_cube_ranges(model: Model, expression: str) -> None:
    model.rule(expression, **SAME_CUBE)
    assert same(assert_settled(model, "Data", [("Year", "2005")]), 90.0)


# ----------------------------------------------------------------- two dimensions


def test_second_dimension_shared_with_the_rule(model: Model) -> None:
    model.rule(
        "SUM(Two::[Year.$<Inputs::P.Start>..$<Inputs::P.End>])",
        cube="OutR", targets=[("Region", "*")], anchored=False,
    )
    for end, north in ((2004, 9.0), (2005, 14.0), (2003, 5.0)):
        model.control("End", end)
        for region, expected in (("N", north), ("S", 100 * north)):
            assert same(assert_settled(model, "OutR", [("Region", region)]), expected)


def test_second_dimension_fixed_in_the_reference(model: Model) -> None:
    model.rule("SUM(Two::[Year.$<Inputs::P.Start>..$<Inputs::P.End>, Region.N])")
    for end, expected in ((2004, 9.0), (2005, 14.0)):
        model.control("End", end)
        assert same(assert_settled(model), expected)


def test_second_dimension_aggregated(model: Model) -> None:
    model.rule("SUM(Two::[Year.$<Inputs::P.Start>..$<Inputs::P.End>])")
    for end, expected in ((2004, 909.0), (2005, 1414.0)):
        model.control("End", end)
        assert same(assert_settled(model), expected)


# -------------------------------------------------------------------- dependencies


def test_dependencies_follow_a_moving_bound(model: Model) -> None:
    model.rule(f"SUM({BOTH})")
    steps: list[tuple[Callable[[], None], float]] = [
        (lambda: None, 90),
        (lambda: model.control("End", 2005), 140),
        (lambda: model.set("Data", [("Year", "2005")], 1000), 1090),  # newly inside
        (lambda: model.set("Data", [("Year", "2001")], 7), 1090),  # outside
        (lambda: model.control("Start", 2005), 1000),
        (lambda: model.set("Data", [("Year", "2003")], 5), 1000),  # formerly inside
        (lambda: model.control("Start", 2001), 1072),  # 7 + 20 + 5 + 40 + 1000
    ]
    for number, (edit, expected) in enumerate(steps):
        edit()
        assert same(model.read(), float(expected)), number
    assert same(assert_settled(model), 1072.0)


def _random_edits(
    model: Model, seed: int, count: int, check: Callable[[str, float], None]
) -> None:
    """Apply recorded-seed edits, comparing each result with a Python reference."""

    rng = SeedStreams(seed).random("phase55.edits")
    data = {year: 10.0 * index for index, year in enumerate(YEARS, start=1)}
    bounds = {"Start": 2002, "End": 2004}
    for number in range(count):
        if rng.random() < 0.5:
            name = rng.choice(["Start", "End"])
            # Reversed bounds are awaiting a contract (D10), so they are not generated.
            lower = 2001 if name == "Start" else bounds["Start"]
            upper = bounds["End"] if name == "Start" else 2005
            bounds[name] = rng.randint(lower, upper)
            model.control(name, bounds[name])
            label = f"{number}: {name}={bounds[name]}"
        else:
            year = rng.choice(YEARS)
            data[year] = float(rng.randint(-100, 100))
            model.set("Data", [("Year", year)], data[year])
            label = f"{number}: {year}={data[year]}"
        expected = sum(
            value for year, value in data.items() if bounds["Start"] <= int(year) <= bounds["End"]
        )
        check(label, expected)


@pytest.mark.parametrize("seed", range(12))
def test_random_edit_sequences_match_reference(model: Model, seed: int) -> None:
    model.rule(f"SUM({BOTH})")

    def check(label: str, expected: float) -> None:
        assert same(model.read(), expected), label

    _random_edits(model, seed, 20, check)
    assert_settled(model)


@pytest.mark.parametrize("seed", range(4))
def test_random_edits_with_unread_steps_match_reference(model: Model, seed: int) -> None:
    """Reading only every third step exercises several pending edits at once."""

    model.rule(f"SUM({BOTH})")
    steps = {"count": 0}

    def check(label: str, expected: float) -> None:
        steps["count"] += 1
        if steps["count"] % 3 == 0:
            assert same(model.read(), expected), label

    _random_edits(model, 100 + seed, 24, check)
    assert_settled(model)


# --------------------------------------------------------------------- cold reload


@pytest.mark.parametrize(
    ("expression", "values"),
    [
        (f"SUM({BOTH})", (90, 140, 1090, 1000)),
        (f"AVG({BOTH})", (30, 35, 272.5, 1000)),
        (f"MAX({BOTH})", (40, 50, 1000, 1000)),
    ],
)
def test_fresh_engine_keeps_value_and_dependencies(
    model: Model, expression: str, values: tuple[float, ...]
) -> None:
    model.rule(expression)
    assert same(model.read(), float(values[0]))
    model.reload()
    assert same(model.read(), float(values[0]))
    model.control("End", 2005)
    assert same(model.read(), float(values[1]))
    model.set("Data", [("Year", "2005")], 1000)
    assert same(model.read(), float(values[2]))
    model.control("Start", 2005)
    assert same(model.read(), float(values[3]))
    model.reload()
    assert same(assert_settled(model), float(values[3]))


# ----------------------------------------------------------------- renames, errors


def test_renaming_the_control_item_keeps_the_reference(model: Model) -> None:
    model.rule(f"SUM({BOTH})")
    model.rename("P", "End", "Finish")
    assert same(assert_settled(model), 90.0)
    model.control("End", 2005)  # the harness key is stable; the item is now "Finish"
    assert same(assert_settled(model), 140.0)


def test_renaming_an_item_then_updating_the_control_recovers(model: Model) -> None:
    model.rule(f"SUM({BOTH})")
    model.rename("Year", "2004", "FY04")
    model.control("End", "FY04")
    assert same(assert_settled(model), 90.0)


@pytest.mark.parametrize("bad", [1999, 2004.5, "zzz", True], ids=repr)
def test_unmatched_control_is_stable_and_recovers(model: Model, bad: Any) -> None:
    """The error's code awaits D5; being a settled error and recovering does not."""

    model.rule(f"SUM({BOTH})")
    model.control("End", bad)
    value = assert_settled(model)
    assert type(value).__name__ == "CellError", value
    model.control("End", 2004)
    assert same(assert_settled(model), 90.0)


@pytest.mark.parametrize(
    "inner", ["Inputs::P.Start..End", "Inputs::P.*"], ids=["range", "wildcard"]
)
def test_multi_cell_bounds_are_rejected(model: Model, inner: str) -> None:
    error = model.try_rule(f"SUM(Data::[Year.2001..$<{inner}>])")
    assert error is not None and "must resolve to a single cell" in error


# ------------------------------------------------------------------------ findings
# Violations of the three checks above. Strict: a repair turns these into failures, so
# the finding has to be closed in plan-v2.md. ``raises`` names the violated check, so a
# broken precondition or any other error is a real failure, not an expected one.


@pytest.mark.xfail(
    strict=True, raises=NotSettled, reason="D6: stale value until full recalculation"
)
def test_finding_rename_of_matched_item_goes_stale(model: Model) -> None:
    model.rule(f"SUM({BOTH})")
    model.recalculate()
    assert same(model.read(), 90.0)
    model.rename("Year", "2004", "FY04")
    assert_settled(model)


@pytest.mark.parametrize(
    "expression",
    ["SUM(Data::[Year.2001..$<Inputs::P.End>])", "SUM(Data::[Year.2001..2004])"],
    ids=["dynamic", "fixed"],
)
@pytest.mark.xfail(
    strict=True, raises=NotSettled, reason="D7: own cell in range depends on history"
)
def test_finding_own_cell_in_range_does_not_settle(model: Model, expression: str) -> None:
    model.rule(expression, cube="Data", targets=[("Year", "2003")])
    assert_settled(model, "Data", [("Year", "2003")])


@pytest.mark.xfail(
    strict=True, raises=NotSubstitutable, reason="D9: XLS_SUM rejects only dynamic ranges"
)
def test_finding_xls_sum_substitution(model: Model) -> None:
    model.rule(f"XLS_SUM({BOTH})")
    model.rule("XLS_SUM(Data::[Year.2002..2004])", targets=[("Line", "R2")])
    fixed = model.read("Out", [("Line", "R2")])
    assert same(fixed, 90.0), f"precondition: fixed-range XLS_SUM gave {fixed!r}"
    actual = model.read()
    if not same(actual, fixed):
        raise NotSubstitutable(f"dynamic {actual!r}, fixed {fixed!r}")


@pytest.mark.parametrize("function", ["SUM", "AVG", "MAX"])
@pytest.mark.xfail(
    strict=True,
    raises=NotSubstitutable,
    reason="D11: parser splits the outer reference at '::' inside $<...>",
)
def test_finding_unqualified_range_with_qualified_bound(model: Model, function: str) -> None:
    model.rule(f"{function}([Year.2002..2004])", **SAME_CUBE)
    fixed = model.read("Data", [("Year", "2005")])
    expected = REDUCERS[function](span(2002, 2004))
    assert same(fixed, expected), f"precondition: fixed range gave {fixed!r}"
    model.rule(f"{function}([Year.2002..$<Inputs::P.End>])", **SAME_CUBE)
    actual = model.read("Data", [("Year", "2005")])
    if not same(actual, fixed):
        raise NotSubstitutable(f"dynamic {actual!r}, fixed {fixed!r}")


# --------------------------------------------------------------- awaiting contracts


def _observe_rule(expression: str, **kwargs: Any) -> Callable[[Model], str]:
    def observe(model: Model) -> str:
        if model.try_rule(expression, **kwargs) is not None:
            return "rejected"
        return repr(model.read())

    return observe


def _observe_emptied(model: Model) -> str:
    model.rule(f"SUM({BOTH})")
    model.control("End", None)
    try:
        return repr(model.read())
    except Exception as exc:  # noqa: BLE001 - the exception type is the observation
        return f"raises {type(exc).__name__}"


def _observe_error_control(extra: str = "") -> Callable[[Model], str]:
    def observe(model: Model) -> str:
        model.rule("1 / Inputs::P.Zero", cube="Inputs", targets=[("P", "Ctl")])
        return _observe_rule(f"SUM(Data::[Year.2001..$<Inputs::P.Ctl>]{extra})")(model)

    return observe


def _observe_unmatched(model: Model) -> str:
    model.control("End", 1999)
    return _observe_rule(f"SUM({BOTH})")(model)


AWAITING: dict[str, tuple[Callable[[Model], str], str]] = {
    "D1-scope-item": (
        _observe_rule("1", cube="Data", targets=[("Year", "$<Inputs::P.End>")]), "rejected"),
    "D1-scope-dynamic-range": (
        _observe_rule("1", cube="Data", targets=[("Year", "2001..$<Inputs::P.End>")]), "rejected"),
    "D1-scope-fixed-range": (
        _observe_rule("1", cube="Data", targets=[("Year", "2002..2004")]), "rejected"),
    "D2-single": (_observe_rule("Data::Year.$<Inputs::P.End>"), "CellError('#REF!')"),
    "D2-single-bracketed": (_observe_rule("Data::[Year.$<Inputs::P.End>]"), "CellError('#REF!')"),
    "D3-dimension": (_observe_rule("Data::$<Inputs::P.End>.2002"), "rejected"),
    "D3-cube": (_observe_rule("$<Inputs::P.End>::Year.2002"), "rejected"),
    "D4-unbracketed-start": (
        _observe_rule("SUM(Data::Year.$<Inputs::P.Start>..2005)"), "rejected"),
    "D5-empty-at-set": (_observe_rule("SUM(Data::[Year.2001..$<Inputs::P.Ctl>])"), "rejected"),
    "D5-emptied-later": (_observe_emptied, "raises RuleValidationError"),
    "D5-unmatched": (_observe_unmatched, "CellError('#EXPRESSION!')"),
    "D5-error-control": (_observe_error_control(), "CellError('#DIV/0!')"),
    "D5-error-control-two-arguments": (
        _observe_error_control(", Data::Year.2001"), "CellError('#REF!')"),
    "D8-if": (
        _observe_rule("SUM(Data::[Year.2001..$<IF(Inputs::P.End = 2004, 2003, 2002)>])"),
        "CellError('#REF!')"),
    "D8-if-literals": (
        _observe_rule("SUM(Data::[Year.2001..$<IF(1, 2003, 2002)>])"), "rejected"),
    "D8-greater-than": (
        _observe_rule("SUM(Data::[Year.2001..$<IF(Inputs::P.End > 1, 2003, 2002)>])"),
        "CellError('#EXPRESSION!')"),
    "D8-nested": (
        _observe_rule(
            "SUM(Data::[Year.2001..$<SUM(Data::[Year.2001..$<Inputs::P.End>])>])"), "rejected"),
    "D9-slice-cross-cube": (_observe_rule(f"SUM(SLICE({BOTH}))"), "0.0"),
    "D9-xls-average": (_observe_rule(f"XLS_AVERAGE({BOTH})"), "CellError('#REF!')"),
    "D10-reversed": (
        _observe_rule("SUM(Data::[Year.$<Inputs::P.End>..$<Inputs::P.Start>])"), "90.0"),
    "ranges-non-sequential": (
        _observe_rule("SUM(KData::[Kind.A..$<Inputs::P.End>])"), "CellError('#EXPRESSION!')"),
}


@pytest.mark.parametrize("case", sorted(AWAITING))
def test_awaiting_contract(model: Model, case: str) -> None:
    """Records current behaviour. Warns on change; asserts nothing until answered."""

    observe, recorded = AWAITING[case]
    actual = observe(model)
    if actual != recorded:
        warnings.warn(
            f"{case}: recorded {recorded!r}, now {actual!r}; update plan-v2.md", stacklevel=1
        )
