# Deterministic Simulation Testing for OM Core: Plan v2

## Open Questions

These questions ask what OM Core is *intended* to do, so that the tests check the right
behaviour. Nothing here is claimed to be a defect. All observations were made on small
models (cubes with one or two dimensions), through the public command session, with serial
recalculation.

### Dynamic bounds: `$<...>` (needed for Phase 5.5)

`Inputs::P.Start` is 2002, `Inputs::P.End` is 2004, and `Year` is a sequential dimension
with items 2001-2005, unless stated otherwise. Where a fixed range such as
`Year.2002..2004` behaves the same way, the question says so.

**D1. Should `$<...>` work in a rule's scope (its left-hand side)?**
`Year.$<Inputs::P.End>` as a target is rejected with "Unknown item", although
`parse_rule_target` accepts `Data::Year.$<Inputs::P.End>` and the bracketed form. A fixed
range as a target (`Year.2002..2004`) is rejected too. If scope is meant to be supported,
should changing the control cell move the rule to a different cell?

**D2. Should `$<...>` work as a single reference, outside a range?**
`Data::Year.$<Inputs::P.End>` is accepted and then evaluates to `#REF!`. Using the bound
at both ends of a range works.

**D3. Should a cell be able to supply a dimension or cube name?**
`Data::$<...>.2002` and `$<...>::Year.2002` are both rejected as syntax errors.

**D4. Must a start bound be in square brackets?**
`Data::[Year.$<a>..$<b>]` and `Data::Year.2001..$<b>` work. `Data::Year.$<a>..2005` and
`Data::Year.$<a>..$<b>` are rejected with a syntax error.

**D5. What should an unusable control value produce?**
- *Empty:* the rule is rejected if the cell is empty when the rule is set. If the cell is
  emptied later, reading the result raises `RuleValidationError` instead of returning an
  error value, while `run_recalculation` reports success. Restoring the value recovers.
- *Not an item (1999), or 2004.5, or `TRUE`:* `#EXPRESSION!`. A fixed range with an
  unknown item gives the same error, but a plain reference such as `Data::Year.1999`
  gives `#REF!`.
- *An error value:* a single-argument aggregate returns the control cell's own error
  (`#DIV/0!` was checked). `SUM` with two cross-cube arguments gives `#REF!` instead,
  although a plain error cell as one of the arguments gives `#DIV/0!`.

**D6. What should happen when the item a control cell names is renamed?**
After renaming 2004 to FY04 while the control cell still holds 2004, the old result
remains until a full recalculation, which then gives `#EXPRESSION!`. A fixed range is
rewritten to the new name and keeps its value. Should the control cell keep matching by
name, and if so, should the error appear immediately?

**D7. Should a range that includes the rule's own cell be `#CIRC!`?**
A rule on `Year.2003` that sums 2001 to 2004 was read as 70 (its own cell left out),
then `#CIRC!` after a full recalculation, then 70 after another. In a second run that
did not read the cell first, one recalculation gave 70, so the result depends on
evaluation history. A fixed range gave the same first two results; the rest was checked
only with `$<...>`. This matters here because editing a control cell can bring a rule's
own cell into its range.

**D8. Which expressions are allowed inside `$<...>`?**
A literal, a reference, and arithmetic (`$<Inputs::P.End - 1>`) work. `IF(...)` gives
`#REF!`, or is rejected with "maximum recursion depth exceeded" when all its arguments
are literals. A `>` inside gives `#EXPRESSION!`, apparently because the first `>` ends
the bound. Nesting one `$<...>` inside another is rejected.

**D9. Which functions should accept a dynamic range?**
`SUM`, `AVG`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, and `COUNTA` work, with one or several
arguments. `XLS_SUM` works with a fixed range but gives `#EXPRESSION!` with a dynamic one.
`XLS_AVERAGE`, `XLS_MAX`, and `XLS_COUNT` give `#REF!` with both. `SLICE` works within the
rule's own cube, but is empty for a range in another cube, fixed or dynamic. From reading
the code, it drops the cube name and discards the resulting error.

**D10. Should reversed bounds be accepted?**
A start after the end is silently swapped, for fixed and dynamic ranges alike.

**D11. Should an unqualified range accept a cube-qualified bound?**
In a rule on `Data`, `SUM([Year.2002..$<Inputs::P.End>])` is accepted and gives
`#EXPRESSION!`. The underlying error is "Unknown cube: 'Year.2002..$<Inputs'": the parser
splits the reference at the first `::`, which is inside the bound
(`lib_openm/rule_eval/parser.py`, line 110). Writing `Data::[...]` avoids it.

### Undo and model changes (needed for Phase 7)

"L" numbers refer to the observations in section 2.3, each made once.

**Q1. Which commands should be undoable?**
Undo and redo work for cell values (L1). After renaming a dimension, cube, or item,
reordering or deleting items, or deleting a cube, `undo` reports success but reverts the
previous value edit, and the structural change stays (L2).

**Q2. What should `undo` do when an earlier edit no longer fits the model?**
For example, after deleting or detaching a dimension, `undo` fails with an address-shape
error (L3). From reading the code, the failed action is then lost from both the undo and
redo history (L4). Should it skip, refuse without changing anything, or clear the history?

**Q3. What should happen to a rule that references a deleted item, dimension, or cube?**
Should it show `#REF!`, be removed, or should the deletion be rejected?

**Q4. When a dimension is attached to or detached from a cube, which values are kept?**
How should several cells collapse into one on detach, and where do existing values go on
attach?

**Q5. Does reordering the items of a set dimension ever affect results?**
Reordering a sequential dimension is refused ("Cannot reorder sequential dimension"), so
sequence references and ranges should not be affected.

**Q6. Must a failed command leave the model unchanged?**

**Q7. Should the overdue deprecated aliases be tested or removed?**
All 24 declare a removal date of 2026-08 or 2026-09 and are still registered (L6). One of
them, `group_create`, also requires two parameters that its handler treats as optional
(L5).

---

## 1. Purpose and Relationship to plan.md

`plan.md` remains the authoritative design record and the completed roadmap for
Phases 0 through 5. Its architecture (sections 1-11), harness shape (section 13), and
definition of done (section 14) continue to apply unchanged. This document continues
the roadmap from Phase 6.

Phases are numbered in execution order, starting with the small Phase 5.5. Phases 5.5, 6
and 7 are planned in implementation detail. Phases 8 to 10 carry their design and
constraints, and each needs its own execution plan before work starts. Section 11 is an
unordered backlog.

Each phase records its decisions, discoveries, and performed validation, including
negative results, in its own outcome section once implemented. Work does not silently
expand into a later phase.

---

## 2. Starting Point

### 2.1 State at the end of Phase 5

- Real-engine deterministic traces with an independent oracle, cache, snapshot,
  persistence, and cold-engine convergence invariants; virtual scheduling; persistence
  faults; OpenM script export.
- Coverage-guided evolution (`lib_dst/evolution.py`, `corpus.py`, `mutation.py`,
  `coverage.py`):
  - **Population:** a coordinator-owned, content-addressed corpus.
  - **Retention:** new normalized branch arcs, new semantic features, or a new failure
    signature.
  - **Selection:** deterministic integer weights from arc, feature, and stratum rarity,
    generations since selection, and inverse size. Failure history has no influence.
  - **Mutation:** twelve recorded, replayable kinds (`hardvalue`, `ast_literal`,
    `ast_operator`, `ast_reference`, `recalculation`, `scheduler_delay`, `fault`,
    `scale_cardinality`, `scale_density`, `scale_rule`, `scale_graph`,
    `operation_burst`), chosen uniformly from those applicable.
  - **Failures:** new signatures are shrunk automatically, in the coordinator process.
- The public `om-dst` CLI (`run`, `replay`, `shrink`), run artifacts with resume, a
  25-entry PR corpus, known-failure expectations, and PR plus nightly workflows.
  Evolution is a library API only.
- Two known command-boundary defects (UDF creation and address formatting), tracked as
  expected failures and not repaired.
- Local throughput (Windows, Python 3.12.1, one worker): about 20 legacy or `small`
  traces per second; worker startup costs about 1 s.

### 2.2 Measured reach

Measured on one machine over 368 traces: legacy seeds 0-299, eight seeds of each of the
six profiles, and the twenty `stress_default` points. Commands were counted as they
crossed `CommandSession.execute`; arcs came from the Phase 4 branch monitor. Coverage
monitoring excludes the scheduled-execution path.

| Measure | Result |
| --- | --- |
| Registered command IDs | 107 (96 distinct handlers) |
| Deprecated aliases (`ALIAS_LIFECYCLE`) | 24, leaving 83 current IDs |
| Commands issued by traces | 13, none deprecated, leaving 70 current IDs unissued |
| Built-in names known to the rule parser (`_FUNCTIONS`) | 113 |
| Built-ins generated | 5 (`SUM`, `AVG`, `IF`, `ROUND`, `ABS`) |
| Cumulative arcs after 1 / 25 / 100 / 300 legacy seeds | 1,338 / 1,476 / 1,520 / 1,520 |
| Cumulative arcs after profiles / after stress points | 1,708 / 1,747 |
| Failures | 2, both already known |

Interpretation, with its limits:

- Legacy seeds 100-299 added no arcs. They still vary values, so they can expose
  numeric defects that arc coverage cannot distinguish.
- No mutation kind introduces a command that its parent does not already use, so
  evolution cannot leave the current command surface.
- Both defects known before this measurement appeared when a command was first
  exercised, not through search. Two cases are suggestive, not statistical evidence.
- Some unissued commands are GUI, navigation, solver, or diagnostic commands and will
  be out of scope. The 70 must be triaged (section 5.2) before being treated as a target.

### 2.3 Lifecycle probe

Each command below was run once through the public session on the model from
`generate_trace(0)`. These are **observations, not confirmed defects**: the repository
documentation searched (`README.md`, `KNOWN_ISSUES.md`, `CHANGELOG.md`, `tech-ref/`)
does not state an undo contract. They are Phase 7 inputs.

| # | Observation | Evidence |
| --- | --- | --- |
| L1 | A hard-value edit undoes and redoes to identical canonical observations. | executed |
| L2 | After `rename_dimension`, `rename_cube`, `rename_dimension_item`, `set_dimension_item_order`, `delete_dimension_items`, or `delete_cube`, `undo` succeeds but reverts the preceding *value* edit; the structural change remains. | executed |
| L3 | After `delete_dimension` or `detach_dimension_from_cube`, `undo` fails with `Address (...) has 2 elements, expected 1 (full) or 0 (short)`: it replays an older cell edit against the changed cube shape. | executed |
| L4 | `UndoManager.undo` and `redo` pop the action before applying it. If applying raises, the action is on neither stack. | code reading (`lib_openm/undo.py`), not executed |
| L5 | `group_create` requires `parent_group_node_id` and `order` at the registry, although `cmd_create_group` gives both defaults. The alias is deprecated in favour of `create_group`. | executed, plus code reading |
| L6 | All 24 aliases declare `remove_after` of 2026-08 or 2026-09 and are still registered. | code reading |
| L7 | Canonical observation addresses objects by name, so it raises `KeyError` after a rename or deletion of an observed dimension or cube. | executed (harness limitation) |

---

## 3. Constraints Carried Forward

1. **Determinism.** Every generated or guided decision is a pure function of recorded
   state and a key-derived seed domain, and is materialized in trace or report metadata.
   No wall-clock time, completion order, PID, or order-dependent floating-point
   accumulation may influence a decision.
2. **Process isolation.** Engine execution, coverage collection, and shrinking happen in
   clean workers. The coordinator is the sole corpus and artifact writer.
3. **Compatibility.** Trace schema version 1 remains readable and replayable, and
   existing traces keep their hashes. New metadata is optional. Versioned corpus and
   mutation records keep a reader for the previous version.
4. **Production surfaces.** Traces use public command boundaries and never reach into
   private engine state. Faulty engine variants for benchmarking exist only in
   explicitly activated, test-only paths.
5. **Evidence over inference.** Volatile measurements never enter identities, ambiguous
   outcomes are never relabelled, budgets come from measurement, and an undocumented
   behaviour is not called a defect until its contract is decided.

---

## 4. Roadmap

| Phase | Content | Depends on |
| --- | --- | --- |
| 5.5 | Dynamic bounds `$<...>` | 5 |
| 6 | Hosted validation, command triage, detection benchmark | 5 |
| 7 | Model lifecycle commands | 6 |
| 8 | Long-running and parallel evolution | 6 (7 recommended first) |
| 9 | Rule language | 6; volatile functions also need injected providers |
| 10 | Guided selection and splicing | 8 and the Phase 6 benchmark |

Repairing the UDF and formatting defects is independent of this order and needs an
explicit decision to put production repairs in scope. UDF generation (Phase 9) is blocked
until the first is repaired.

### 4.1 Phase 5.5: Dynamic bounds `$<...>`

A small, self-contained phase, done before anything else. The feature has no existing
tests or example scripts in this repository (searched in `tests/`, `examples/`, and the
GUI, REPL, and TUI packages).

It is small, but not a single function. `_resolve_range_bound` evaluates a bound, and is
called from three separate range paths (`sum_over_ref`, the other aggregates, and
`slice_over_ref`). The tokenizer, the reference parser, and rule validation each handle
the syntax separately. The current trace generators produce no ranges, and the typed AST
has no range node.

**Step 1: direct tests**, through the public session, on hand-built models. No generator
changes. Three checks need no answers from the questions above:

1. *Substitution:* an expression using `$<cell>` equals the same expression with the
   cell's value written as a literal. This is the main oracle, and it applies wherever
   both forms are accepted.
2. *Recalculation:* after any edit, the value read incrementally equals the value after a
   full recalculation, and repeating the recalculation changes nothing.
3. *Cold reload:* the value survives save and load into a fresh engine, and later edits to
   the control cell and to cells newly inside the range still take effect.

Coverage: each bound position; text, number, and rule-computed controls; moving a bound so
that cells enter and leave the range; edits to cells inside, formerly inside, and outside
the range; one- and two-dimensional cubes, with the second dimension shared, fixed, or
aggregated; each accepted function; and each error case in D5.

Behaviour that depends on D1-D11 is written down with its current result and marked as
awaiting an answer. It becomes a pass or fail expectation only once answered. D6, D7, the
`XLS_SUM` part of D9, and D11 break a check above; they are reported as findings, not
hidden as expectations.

**Step 2: generated traces.** Add range references with fixed and dynamic bounds to a new
`dynamic_bounds` profile, with the substitution check as its oracle. Schema version 1 and
existing trace hashes are unchanged. Start only after step 1 passes or its failures are
recorded.

**Files:** `tests/test_dst_phase55.py` (new) for step 1. For step 2, `lib_dst/ast.py`,
`oracle.py`, `scale_generator.py`, `profiles.py`, `validation.py`, and new corpus entries
through `scripts/dst_build_corpus.py`.

**Exit:** every accepted form in D1-D11 is covered or explicitly awaiting an answer; each
finding has a minimal reproducible case; and the standard validation passes with existing
trace hashes unchanged.

#### Phase 5.5 step 1 outcome (implemented 2026-11-23)

- Added `tests/test_dst_phase55.py`: 120 tests, of which 113 pass and 7 are strict expected
  failures. No production or `lib_dst` code changed.
- Findings, as strict `xfail` tests: D6 (1), D7 (2, dynamic and fixed), `XLS_SUM` in D9 (1),
  and D11 (3 functions). Each names the violated check through `raises`, so a broken
  precondition is a real failure. A repair makes the test fail until it is updated.
- Behaviour awaiting an answer is recorded in `AWAITING` and only warns when it changes.
- Detection was checked by loading `_engine_core.py` with one textual fault at a time,
  without changing files. All nine faults were detected, including an off-by-one in each of
  the three range functions, a stale bound, a missing invalidation, and a leaked evaluation
  flag. An unmodified control run matched the normal result.
- The first version of the tests never reached `slice_over_ref`. Counting calls showed it;
  aggregates with several cross-cube arguments do, and are now covered. D11 was found while
  adding same-cube cases.
- Results were identical under `PYTHONHASHSEED` 0, 1, and 777.
- Not covered: cubes with more than two dimensions, sequence keywords as bounds, unqualified
  references inside `$<...>`, conditional aggregates, and the GUI, REPL, and remote paths.
  Step 2 has not started.
- Validation: `pytest tests -q` gives 284 passed and 7 xfailed; corpus `--check`,
  `compileall`, and `git diff --check` pass.

---

## 5. Phase 6: Foundations

### 5.1 Phase 5 carry-over

- Run `dst-pr.yml` and `dst-scheduled.yml` on hosted runners. Confirm that the 8192 MB
  `--memory-mb` limit does not trip on Linux address-space reservations.
- Record hosted throughput per profile and a Linux `stress_default` identity baseline.
- Re-size the nightly seed counts from hosted throughput and section 2.2: move budget
  from legacy seeds, which add no arcs after about seed 100, to profile seeds. Keep some
  legacy seeds for value variation.

### 5.2 Command triage

Classify every current command ID, in a checked-in table, as one of:

- **in scope:** deterministic, reachable through `CommandSession`, observable model
  effect;
- **deferred:** needs a boundary that does not exist yet (solver, remote, GUI, import);
- **excluded:** navigation, view state, profiling, or diagnostics with no model effect;
- **alias:** tested through its replacement.

A test fails when a registered command is missing from the table, so new commands cannot
go untracked silently. Classification requires reading or executing the handler, not
inferring from the name.

### 5.3 Detection benchmark

Measures which faults the harness can see, before anything is added.

- **Engine mutants.** A test-only registry of 6-10 small, deliberate semantic faults
  applied inside a worker only, activated by a hidden flag, and never enabled in a normal
  campaign. Candidates: off-by-one dependency invalidation; a dropped dirty mark after a
  rule edit; wrong `#DIV/0!` propagation through `IF`; skipped cache invalidation on a
  hard-value edit; a rounding-mode change in `ROUND`; rule precedence lost on save/load;
  stale references after a rename; stale dependencies after an item is deleted.
- **Protocol.** For each mutant, run the fixed corpus, a fixed seed budget, and every
  profile. Record whether it is detected, by which invariant, and the executions to first
  detection over at least 10 seed windows. A mutant not found within budget is reported
  as not detected, never dropped.
- **Validity.** Each mutant needs a hand-written trace or direct test showing that it
  changes observable behaviour; otherwise it is equivalent and is removed.
- **Use.** Undetected mutants are gaps. The lifecycle mutants are expected to be
  undetected until Phase 7, whose acceptance includes detecting them.

This trimmed benchmark needs no `evolve` command. Comparing search strategies is
Phase 10 work (section 9.4).

### 5.4 Files, tests, and exit

- Files: `lib_dst/benchmark/` (new), `lib_dst/worker.py` (hidden mutant flag),
  `tests/dst_command_triage.json` (new), `tests/test_dst_phase6.py` (new), and
  `.github/workflows/dst-scheduled.yml`.
- Tests: the triage table is complete; an inactive registry leaves corpus identities
  unchanged; every mutant is non-equivalent; benchmark reports repeat exactly.
- Exit: both workflows have completed on hosted runners with recorded budgets; every
  command is classified; and the per-mutant detection report is recorded here.

---

## 6. Phase 7: Model Lifecycle Commands

### 6.1 Goal and non-goals

Generate traces that change existing models, where stale caches, dependency edges, and
persisted references can hide. `KNOWN_ISSUES.md` names grouping, aggregation, ordering,
sparse data, and rule precedence as areas needing more tests.

Non-goals: GUI, navigation, or view-state commands; solver and import commands; repairing
production defects; new rule-language functions.

### 6.2 Contracts to decide first

The contract questions for this phase are Q1-Q7 at the top of this document, with space
for the answers. Q1-Q2 decide the undo invariant, Q3-Q4 the oracle values, Q5 the
reorder invariant's precondition, Q6 whether failure atomicity is an invariant, and Q7
triage.

Phase 7 asserts nothing about a command until its question is answered. Until then the
command may be executed for crash and convergence checks, but not for value expectations.

### 6.3 Command batches

Each batch lands separately, with its own oracle support, generator, and shrinking.

| Batch | Commands |
| --- | --- |
| 7a Values and rules | `clear_cell_hardvalue`, `update_rule`, `delete_rule`, `set_rule_anchored`, `delete_rule_anchored`, `set_rule_order`, `undo`, `redo` |
| 7b Names and order | `rename_dimension`, `rename_dimension_item`, `rename_cube`, `set_dimension_item_order` |
| 7c Structure | `delete_dimension_items`, `delete_dimension`, `attach_dimension_to_cube`, `detach_dimension_from_cube`, `create_aggregate_item` |
| 7d Groups | `create_group`, `move_items_to_group`, `ungroup_items`, `rename_group_node`, `delete_group` |
| 7e Mid-trace persistence | `create_checkpoint`, `restore_checkpoint`, `load_workspace` between edits |

The lists are provisional until triage confirms each handler.

### 6.4 Harness changes

- **Shadow model.** A harness-owned logical model tracks names, order, membership, rules,
  and values after every operation. It replaces the assumption that a symbol, once
  created, never changes. It shares no code with the engine.
- **Symbols.** A logical handle stays stable across renames, and its current name is
  separate. Deleted handles become tombstones; validation rejects later use except where
  a trace deliberately tests a missing reference.
- **Observation.** Resolve L7 by recording observation specs against final logical names.
  Schema-1 traces without lifecycle operations keep their exact current form and hash.
- **Validation, mutation, shrinking.** Dependency checks understand rename and delete.
  Mutations may insert, remove, or move lifecycle operations only while the trace stays
  valid. Shrinking must not turn a deletion failure into a missing-symbol failure.
- **Script export.** Operations that OpenM scripts cannot express are rejected
  explicitly, as today. JSON remains the replay authority.

### 6.5 Invariants

All apply in addition to the existing oracle and cold-convergence checks.

1. **Build equivalence:** the edited model equals a fresh model built directly from the
   shadow model's final state. This is the main stale-state detector.
2. **Rename:** values are unchanged; only names differ.
3. **Reorder:** values by name are unchanged where the contract says order is not
   semantic.
4. **Undo/redo:** for undoable commands, undo restores the prior observation and redo
   restores the later one, including after unrelated edits.
5. **Failure atomicity:** a rejected command leaves the observation unchanged, if that
   contract is adopted.
6. **Checkpoint:** restoring returns to the checkpointed observation, and later edits
   behave as they would on a model freshly built in that state.

### 6.6 Files, tests, and exit

- Files: `lib_dst/shadow.py` (new), `lifecycle_generator.py` (new), `executor.py`,
  `symbols.py`, `observation.py`, `validation.py`, `invariants.py`, `mutation.py`,
  `shrinker.py`, `profiles.py` (a `lifecycle` profile), `tests/test_dst_phase7.py` (new),
  and `scripts/dst_build_corpus.py` (new corpus entries).
- Tests: existing trace hashes and corpus results are unchanged; the shadow model agrees
  with hand-computed cases for every command; generation, replay, mutation, and shrinking
  are deterministic; each invariant fails on a seeded violation; L1-L5 have replayable
  traces whose expectations follow the decided contracts.
- Exit: every in-scope command in batches 7a-7e is generated and checked or explicitly
  deferred with a reason; the lifecycle mutants from section 5.3 are detected; new
  failures are minimized, classified, and recorded; and the section 2.2 measurement is
  repeated so the gain in commands and arcs is known.

---

## 7. Phase 8: Long-Running and Parallel Evolution

### 7.1 `om-dst evolve` and a persistent corpus

```text
om-dst evolve --corpus-dir DIR --master-seed S --iterations N
              [--initial-seeds A:B] [--initial-profile P:A:B] [--initial-corpus DIR]
              [--jobs K] [--timeout T] [--memory-mb M] [--known-failures FILE]
              --out DIR [--resume|--overwrite]
```

- **Directories.** The run directory holds reports, failure artifacts, and measurements.
  The corpus directory is long-lived state, separate from run artifacts.
- **Worker boundaries.** Candidates run through `process.run_worker`, so a crash or
  timeout is classified instead of raising `CalledProcessError` (Phase 4's
  `run_candidate_in_worker` uses `check=True`). Shrinking moves into a worker, and each
  minimized trace is replayed in a clean worker before it is stored.
- **Crash signatures.** Abnormal exits and timeouts get normalized signatures marked
  host-dependent, excluded from cross-platform comparison.
- **Known-signature suppression.** Signatures from `known_failures.json`, corpus
  expectations, or `--known-signature` are recorded and counted, but are not new
  failures. Suppression is always visible in the report.
- **Corpus schema version 2,** readable from version 1, adding per-revision coverage
  provenance and a known-signature flag.
- **Coverage staleness.** Arcs are keyed by source-line transitions, so a production edit
  shifts them. When the recorded revision differs, existing entries are re-executed to
  refresh coverage before selection.
- **Culling.** A deterministic reduction keeps a small set of entries preserving the
  union of arcs, features, and signatures. The decision is recorded.
- **Persistence.** Recommended: Actions cache plus periodic artifact snapshots. Losing
  the cache loses progress, never correctness. A bot-written branch would need
  `contents: write`.

### 7.2 Parallel workers

`--jobs K` for `evolve` and `run --seeds`.

- Seed results aggregate in seed order; summary identity is independent of K and of
  completion order.
- Evolution selects K parents and mutations from the state at the start of a generation,
  executes them in parallel, then applies updates in candidate-index order. Results are
  independent of completion order, but K is part of campaign identity: K=4 legitimately
  differs from K=1.
- Workers stay separate processes with no shared runtime state.

### 7.3 Corpus analytics

`om-dst corpus-stats --corpus-dir DIR`: a deterministic, analysis-only report of feature,
stratum, and signature distributions, mutation-kind yield, lineage depth, and features
that co-occur with failures.

### 7.4 Tests and exit

- The version-1 corpus loads unchanged. With one job and with refresh and culling
  disabled, selections, mutation decisions, and retained traces match Phase 4 exactly,
  and the new index projected onto version-1 fields equals the old one.
- Repeated runs are identical for a fixed K, including with deliberately perturbed
  completion order. Seed-run identity matches for K=1 and K=4.
- Crashing and timing-out candidates are classified and do not abort the campaign.
- Suppressed signatures are counted but not novel. Refresh and culling preserve the
  union of arcs, features, and signatures.
- Exit: `evolve` runs nightly from a persistent corpus and is reproducible from its
  recorded inputs, and the throughput gain from `--jobs` is measured.

---

## 8. Phase 9: Rule Language

Widen expressions in batches, each with an independent oracle or a stated metamorphic
contract (plan.md section 7.3). Parser acceptance is not semantic evidence.

| Batch | Content |
| --- | --- |
| 9a | Logic and scalar math: `AND`, `OR`, `NOT`, `IFERROR`, `MIN`, `MAX`, `MOD`, rounding family, powers and logarithms |
| 9b | Remaining range forms, counts, and conditional aggregates (`COUNT*`, `SUMIF*`); basic and dynamic ranges come from Phase 5.5 |
| 9c | Sequence references (`Year[PREV]`) and hierarchy navigation |
| 9d | Strings and dates |
| 9e | `XLS_*` compatibility functions; document where they intentionally differ from the native ones |
| 9f | Volatile functions (`NOW`, `TODAY`, `RAND`, `RANDBETWEEN`), only after they use the injected providers |
| 9g | UDFs, only after the known facade defect is repaired |

The 113 parser names include aliases and context functions, so each batch starts by
triaging its names as section 5.2 does for commands. Error propagation and domain edges
are generated deliberately. Transcendental functions use stated tolerances.

Exit: each supported function has an oracle, error cases, and at least one detectable
mutant; unsupported names are listed explicitly; section 2.2 is measured again.

---

## 9. Phase 10: Guided Selection and Splicing

Guidance is added last because it needs a wide input space to search and a benchmark to
justify it. `evolve` gains `--strategy baseline|guided`; baseline stays the default until
section 9.4 supports changing it.

### 9.1 Failure-aware selection

- **Lineage.** Entries record integer counts of descendants tested, retained, and
  producing new signatures, updated in candidate-index order.
- **Lineage boost.** A capped, diminishing (integer-log) weight rewards parents whose
  descendants found new arcs or unsuppressed signatures.
- **Near-miss signals (optional).** Deterministic measures only, such as oracle error
  near tolerance, depth near a recorded limit, dirty-frontier size, or payload growth.
  Each is a separately switchable term.
- Every term's contribution is recorded in the selection decision.

### 9.2 Adaptive mutation choice

A deterministic bandit (fixed-point upper-confidence bound, with an exploration floor)
replaces uniform choice, using recorded yield per kind. The chosen kind and a digest of
its score table are recorded; replay consumes the decision and never re-runs the bandit.
New `MutationDecision` fields are optional.

### 9.3 Splicing

- `splice_ast`: a type-compatible subtree from a second parent, using only references
  that resolve in the first.
- `splice_operations`: a dependency-valid operation burst between traces with the same
  resolved topology, including lifecycle operations from Phase 7.

Both parents, the partner seed domain, paths, and replacement are recorded. Candidates
pass structural validation and the parser gate before execution.

### 9.4 Strategy comparison

For each Phase 6 mutant, strategy, and at least 10 master seeds, run with an equal
execution budget. Record executions to first detection, with not-found runs reported.
Compare medians and detection rates per mutant, keep every per-seed result, and test each
term alone. A term that does not help stays disabled. The benchmark is opt-in and never
runs on pull requests.

### 9.5 Tests, risks, and exit

- Two runs with the same corpus, seed, strategy, and K are identical, and every recorded
  mutation or splice replays exactly.
- Risks: overfitting to a few mutants (keep them diverse, prefer simple terms); sparse
  failures (coverage stays the primary signal); CI cost (manual benchmark).
- Exit: the default strategy is chosen from recorded evidence, including terms that did
  not help.

---

## 10. Working Agreements

- Each phase, and each batch within Phases 7 and 9, starts with a concrete execution plan
  (files, interfaces, compatibility, tests, validation commands, unresolved decisions).
- Standard validation applies throughout: `pytest tests -q`, `compileall`,
  `git diff --check`, and CLI subprocess-boundary tests for new commands.
- Generated corpora, benchmark reports, and run artifacts are published by CI and not
  committed, except curated fixtures such as `tests/dst_corpus/`.

### Decisions to settle

| Decision | Recommendation | Needed by |
| --- | --- | --- |
| Dynamic-bound contracts (D1-D11) | OM Core creator decides each; record at the top | Phase 5.5 |
| Mutant location | Test-only package, never imported by production modules | Phase 6 |
| Nightly seed mix | Shift budget from legacy to profile seeds; keep some legacy | Phase 6 |
| Lifecycle contracts (Q1-Q7) | OM Core creator decides each; record at the top | Phase 7 |
| Production repairs (UDF, formatting, and any confirmed L-item) | Decide per defect | Any time; UDF before 9g |
| Corpus persistence | Actions cache plus snapshots | Phase 8 |
| Crash signatures in the corpus | Retain, flagged host-dependent | Phase 8 |
| `--jobs` identity | K is part of campaign identity | Phase 8 |
| Default strategy | Baseline until the benchmark supports a change | Phase 10 |

---

## 11. Backlog (not yet planned)

- **Serial versus multithreaded recalculation.** First as a comparison of final results,
  labelled non-deterministic stress and kept out of replayable claims; controlled
  interleaving only after a scheduler boundary exists.
- **Remote engine, then solver, then GUI event loop,** one at a time, each after its
  deterministic boundary exists. Remote only if it is meant to ship.
- **Persistence depth:** loading older file versions and corruption beyond the three
  current faults.
- **Windows memory enforcement** through Job Objects.
- **Cross-platform differential testing** between Windows and Linux.
- **Learned candidate ranking,** only with dozens of distinct failures, and only if it
  beats the best Phase 10 strategy on the benchmark.
- **Campaign operations:** corpus sharing, triage dashboards, and drafted issues with
  human review before filing.
