# Deterministic Simulation Testing for OM Core

## 1. Purpose and Scope

This document defines an implementation plan for application-level Deterministic
Simulation Testing (DST) of OM Core. The harness will generate valid workspaces and
command sequences, execute them reproducibly, and verify engine behavior with
differential, metamorphic, replay, and fresh-engine convergence checks.

The first implementation targets the local Python engine in serial mode. OM Core's
local command bus is currently synchronous, while solver jobs, remote-engine socket
traffic, subprocesses, and multithreaded recalculation introduce separate concurrency
models. Those systems are added only after their clock, transport, and scheduling
boundaries are explicitly injectable.

The target is reproducibility of observable behavior, not byte-for-byte identity of
runtime internals. A failure is reproducible when the same environment descriptor and
trace produce the same canonical result, error, and invariant violation.

### Initial scope

- Local Python `Engine` and a newly constructed `Workspace`.
- Serial recalculation and dependency tracking.
- Commands executed through a real `CommandSession` where the command is supported.
- In-memory workspace persistence and fresh JSON round trips.
- Valid OM Core rule expressions and `CellError` values.
- Process-isolated seeds with deterministic trace replay.

### Deferred scope

- Remote engine and socket transport.
- Solver subprocesses and third-party numerical backends.
- GUI event-loop scheduling.
- Multithreaded recalculation.
- Arbitrary filesystem virtualization.

These are not considered deterministic until they receive the same provider and
scheduler boundaries described below.

---

## 2. OM Core Constraints That Shape the Harness

The design must preserve the behavior of the existing runtime rather than model a
different asynchronous application.

1. The in-process `MessageBus` publishes synchronously and invokes subscribers inline.
2. `MessageBus.request()` waits on a real `threading.Event`; delayed virtual delivery
   cannot be introduced without changing request pumping.
3. Several command and engine components obtain the global bus directly.
4. The dependency graph stores edges and dirty nodes in sets.
5. IDs, timestamps, random functions, session metadata, and volatile formula functions
   currently call standard-library providers directly in multiple modules.
6. `PYTHONHASHSEED` is fixed when a Python interpreter starts.
7. Full recalculation can reuse an existing dependency graph; it is not equivalent to
   constructing a fresh engine from serialized workspace state.
8. Runtime composition currently creates JSON and SQLite persistence adapters directly,
   although in-memory workspace adapters already exist.

Consequently, the harness uses worker processes for environment isolation, disables
multithreaded calculation initially, and adds narrow dependency-injection seams before
simulating bus latency or reordering.

---

## 3. Architecture

```text
Master seed + environment descriptor
                  |
                  v
       Key-derived independent seeds
       |          |          |
       v          v          v
   workload   scheduler    faults
       |          |          |
       +----------+----------+
                  v
        Operation trace generator
                  |
                  v
       Deterministic worker process
       - real Workspace and Engine
       - real CommandSession
       - serial calculation
       - in-memory persistence
       - optional virtual scheduler
                  |
                  v
         Canonical observation
                  |
          +-------+--------+
          |       |        |
          v       v        v
       oracle  metamorphic fresh engine
```

The operation trace is the source of truth. Bus traffic is diagnostic evidence, not the
replay format, because message envelopes contain runtime context objects and generated
metadata that are neither stable nor safely serializable.

---

## 4. Deterministic Context and Process Isolation

### 4.1 Seed derivation

A master seed derives independent sub-seeds using a stable keyed hash. Adding a random
draw to one domain must not perturb another domain.

| Domain | Controls |
| --- | --- |
| `environment` | Worker environment, initial logical epoch, hash seed. |
| `schema` | Dimension, item, cube, and rule topology. |
| `values` | Hard values and formula constants. |
| `operations` | Command selection and target selection. |
| `scheduler` | Equal-time ordering, delays, and permitted yields. |
| `faults` | Fault site, occurrence count, and fault outcome. |
| `mutation` | Trace mutation and shrinking choices. |

Use SHA-256 or BLAKE2b over an unambiguous encoding such as
`dst-v1\0<master-seed>\0<domain>`. Record the derivation version in every trace.

### 4.2 Worker model

Each seed runs in a fresh worker process, or in a worker that performs a proven complete
runtime reset between seeds. The initial implementation should prefer one process per
seed batch and periodically recycle workers.

Before process start, set:

- `PYTHONHASHSEED` from the environment sub-seed;
- a locale and timezone recorded in the environment descriptor;
- OM Core debug/configuration flags explicitly;
- calculation mode to serial;
- remote-engine and solver execution off unless a scenario explicitly targets them.

Parallelism means multiple isolated worker processes. It does not mean multiple DST
scenarios sharing the singleton bus, gateway, service registry, or engine in one Python
interpreter.

### 4.3 Injectable providers

Add a small `RuntimeProviders` dependency at the runtime composition root:

```python
@dataclass(frozen=True)
class RuntimeProviders:
    clock: Clock
    ids: IdProvider
    random: RandomProvider
```

Provider responsibilities:

- `Clock`: wall time, monotonic time, and sleep/deadline behavior.
- `IdProvider`: workspace/model IDs, session IDs, message IDs, and correlation IDs.
- `RandomProvider`: volatile rule functions and any engine-generated random data.

Do not monkeypatch individual modules as the long-term architecture. A short Phase 0
spike may monkeypatch to inventory call sites, but replay correctness must rely on
explicit providers.

### 4.4 Stable iteration

Sort at nondeterministic boundaries, particularly:

- dependency graph `dirty_keys()`, `precedents_of()`, and `dependents_of()`;
- ready/frontier queues with a documented tie-break key;
- generated symbol tables and trace serialization;
- canonical workspace observations.

Sorting only the workload generator is insufficient because graph sets influence
recalculation order. The stable order must not change rule precedence semantics.

---

## 5. Runtime and Scheduler Integration

### 5.1 Stage A: deterministic synchronous execution

The first useful harness keeps the existing bus synchronous. It generates an operation,
executes it to completion through the real session, records the result, and then advances
to the next operation. This covers schema mutation, values, rules, incremental
recalculation, save/load, and error contracts without pretending that the local bus is
asynchronous.

### 5.2 Stage B: injectable bus

Refactor runtime construction so it can accept:

- `MessageBus`;
- workspace and snapshot persistence adapters;
- `RuntimeProviders`;
- optional services such as timeline and solver.

Remove direct `get_message_bus()` calls from execution paths used by DST by threading the
injected bus through the executor, publishers, gateway, query service, and command
service. Production defaults continue to use the existing singleton.

### 5.3 Stage C: scheduler-aware request/reply

Introduce a scheduler contract rather than placing a delayed queue behind the existing
blocking `request()` method:

```python
class Scheduler(Protocol):
    def call_at(self, when_ns: int, callback: Callable[[], None]) -> EventId: ...
    def run_next(self) -> bool: ...
    def run_until(self, predicate: Callable[[], bool], deadline_ns: int) -> bool: ...
```

The simulated request path must pump scheduled events until its correlated reply arrives
or the virtual deadline expires. It must never wait on a real `threading.Event`. Queue
items use `(logical_time_ns, deterministic_sequence, event_id)` as their total ordering.
Use integer nanoseconds rather than floating-point timestamps.

Only after this seam exists may scheduler scenarios delay or reorder messages. Reordering
must respect explicitly declared causal constraints, such as a reply occurring after its
request handler.

---

## 6. Persistence Isolation

Use `InMemoryJsonAdapter` for the normal harness. It exercises OM Core's production JSON
serialization shape while avoiding workspace files. Add in-memory snapshot/timeline
adapters where timeline commands are in scope.

Runtime factory functions must accept adapters instead of always creating
`JsonFileAdapter` and `SQLiteSnapshotStoreAdapter`. Configuration, global UDF discovery,
logs, and subprocess artifacts are disabled or redirected to a per-worker temporary
directory.

A general virtual filesystem is not required for the initial harness. Prefer explicit
ports for workspace persistence, snapshots, configuration, and UDF discovery. This
makes fault sites precise and keeps production I/O behavior testable.

Storage faults are injected by a decorator around an adapter, for example:

- fail before write;
- fail after serialization but before commit;
- return a truncated or invalid payload on load;
- fail on the Nth operation.

Every storage fault must declare its expected contract: atomic preservation, a structured
command error, or successful recovery. Do not assert convergence after a fault whose
documented semantics permit lost state.

---

## 7. Workload Generation

### 7.1 Symbol table

Maintain a typed symbol table updated only after successful operations:

```text
Workspace
|- Dimensions
|  |- Region: US, EU, APAC
|  `- Year: 2025, 2026
|- Cubes
|  |- Sales(Region, Year)
|  `- Expenses(Region, Year)
`- Rules and address masks
```

Store stable logical handles in traces rather than generated runtime UUIDs. During
execution, resolve handles such as `cube:sales` or `item:region/us` to the IDs created by
that worker's `IdProvider`.

### 7.2 OM Core expression grammar

Generate OM Core AST nodes, then serialize them using OM Core syntax. Examples:

- same-cube reference: `[Region.US, Year.2026]`;
- cube-qualified reference: `Sales::[Region.US, Year.2026]`;
- dimension item reference: `Region.US`;
- sequential accessor: `Year[PREV]`;
- functions: `SUM(...)`, `AVG(...)`, `IF(...)`, `ROUND(...)`.

Do not generate spreadsheet syntax such as `Sales[Region='US']`.

Use the production tokenizer/parser as a validation gate before applying a generated
rule. Parser acceptance establishes syntax validity; the separate reference interpreter
still establishes semantic expectations for its supported subset.

### 7.3 Typed, depth-bounded generation

Each AST node carries an output type and domain restrictions. Initially support:

- finite numeric literals;
- numeric cell references;
- `+`, `-`, `*`, and guarded `/`;
- numeric comparisons;
- `IF` with compatible branches;
- `SUM`, `AVG`, and `ROUND` for cases the oracle models exactly.

At depth 0-1, prefer references and binary expressions. At deeper levels, increase the
probability of literals and references and enforce a hard maximum node count. Record the
AST in the trace, not only its rendered string.

Expand later to strings, ranges, sequential accessors, hierarchy navigation, volatile
functions, UDFs, and error-producing expressions. Each expansion requires matching
oracle or metamorphic semantics.

### 7.4 Scenario modes

1. `acyclic_numeric`: references only previously available cells/cubes.
2. `incremental_graph`: edit precedents after dependency edges have been established.
3. `cycle_contract`: deliberately create self and multi-cell cycles and expect `#CIRC!`.
4. `error_contract`: missing references, division by zero, syntax rejection, and shape
   errors with the appropriate `CellError` or command failure.
5. `persistence_roundtrip`: serialize, deserialize, reconstruct, and compare.
6. `scheduler_contract`: enabled only after the scheduler-aware bus exists.
7. `fault_contract`: applies only named, documented transport or storage faults.

---

## 8. Trace Format and Replay

Use a versioned JSON schema:

```json
{
  "schema_version": 1,
  "seed_derivation_version": "dst-v1",
  "master_seed": "0x8f3a219b",
  "environment": {
    "python": "3.12",
    "python_hash_seed": 12345,
    "timezone": "UTC",
    "engine_mode": "python",
    "recalculation": "serial"
  },
  "operations": [
    {
      "step": 0,
      "kind": "create_dimension",
      "target": "dimension:region",
      "args": {"name": "Region"}
    }
  ]
}
```

An operation records logical targets, normalized arguments, expected fault decisions,
and optional virtual timing. Do not serialize live execution contexts, callbacks, bus
subscriber identities, or arbitrary Python objects.

Replay modes:

- `seed`: regenerate and execute, then optionally verify the generated trace hash;
- `trace`: execute the recorded operations without consulting workload PRNGs;
- `prefix`: execute through step N for debugging and shrinking.

Record the repository revision, trace schema version, Python version, relevant dependency
versions, and OM Core configuration. A replay tool must reject or clearly warn about an
incompatible environment rather than silently claiming determinism.

---

## 9. Canonical Observations and Invariants

### 9.1 Canonical observation

Compare user-visible semantic state, not raw object graphs. The canonical snapshot
contains:

- dimensions, items, cubes, and rules keyed by logical name/handle;
- ordered dimension membership and rule precedence where order is semantic;
- hard values and calculated values for the scenario's observation set;
- `CellError.code` for error cells;
- command outcome and normalized error category;
- dependency relationships only in graph-specific invariants, sorted canonically.

Exclude generated IDs, message/correlation IDs, timestamps, logs, caches, profiling
durations, session access times, and incidental dictionary order.

### 9.2 Differential oracle

The reference interpreter must be independent of OM Core's evaluator. It implements only
the declared typed subset and models OM Core error propagation deliberately. Never use
the same evaluator for the system under test and the oracle.

Numeric comparison uses:

```python
math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
```

Tune tolerances per operation when aggregation order or external numeric libraries make
that necessary. Compare integers and error codes exactly. Handle signed zero, NaN, and
infinity explicitly rather than allowing them through ordinary tolerance checks.

### 9.3 Metamorphic invariants

Apply transformations only when their preconditions hold:

- finite numeric `E`: `E + 0 == E` and `E * 1 == E`;
- finite, nonzero numeric `E`: `E / E == 1` within tolerance;
- compatible pure branches: `IF(True, E1, E2) == E1`;
- commutative finite operands: `A + B == B + A` within tolerance;
- substitution: replace pure scalar subexpression `E` with its oracle value.

Exclude volatile functions, errors, strings, side effects, unstable overflow cases, and
context-dependent range expressions unless a transformation explicitly supports them.

### 9.4 Incremental versus fresh-engine convergence

1. Execute the operation trace incrementally in Workspace A.
2. Serialize Workspace A through `InMemoryJsonAdapter`.
3. Deserialize into a new Workspace B.
4. Construct a new Engine B with empty caches and a new dependency graph.
5. Run full bootstrap/recalculation in serial mode.
6. Compare canonical observations of A and B.

This is the cold convergence invariant. Calling `recalculate_all()` twice on Engine A is
a separate cache/idempotence invariant, not a cold-engine test.

### 9.5 Error contracts

Expected engine values include:

| Condition | Expected result |
| --- | --- |
| Division by zero | `CellError("#DIV/0!")` |
| Missing or invalid reference | `CellError("#REF!")` or validation rejection, depending on operation boundary |
| Circular cell dependency | `CellError("#CIRC!")` |
| Invalid expression syntax | `CellError("#SYNTAX!")` or command validation failure |
| Numeric domain/overflow | `#NUM!` or `#RANGE!` according to the function contract |

The invariant is both the correct structured result and absence of an unhandled process
failure. Do not expect a `CircularDependencyError`; OM Core exposes circular calculation
errors as `#CIRC!` at the cell boundary.

---

## 10. Fault Model

Fault injection is contract-based. A fault point must name the layer, operation, delivery
guarantee, expected recovery behavior, and whether state loss is allowed.

### Supported initial faults

- persistence adapter failure on the Nth save/load;
- malformed persisted JSON on load;
- command handler raises a declared domain exception;
- virtual request timeout after scheduler integration.

### Later transport faults

- delayed event notification;
- dropped non-mutating observation event;
- duplicate delivery of an explicitly idempotent message;
- reordered independent events.

Do not initially duplicate or drop arbitrary mutation commands. OM Core currently has no
general idempotency key or retry contract, so repeated mutations or lost requests may be
valid outcomes rather than engine defects. Add command-level idempotency semantics before
asserting exactly-once behavior.

Every fault decision is stored in the trace. Replay consumes the stored decision instead
of drawing again from the fault PRNG.

---

## 11. Mutation, Shrinking, and Coverage

### 11.1 Trace mutation

Mutators operate on structured operations and ASTs while maintaining symbol-table and
type validity:

- replace a numeric literal with boundary values;
- replace an operator with a type-compatible operator;
- replace a reference with another compatible existing symbol;
- insert/delete a command when its dependencies remain satisfied;
- shift virtual timing after scheduler integration;
- change one recorded fault decision.

Validate the mutated trace before execution.

### 11.2 Shrinking

Shrink in this order:

1. remove operation ranges using delta debugging;
2. remove independent schema objects;
3. reduce dimension sizes and observation sets;
4. simplify AST nodes to children, references, or literals;
5. reduce numeric values toward `0`, `1`, and nearby boundaries;
6. remove scheduler perturbations and faults.

A candidate is retained only if it reproduces the same normalized failure signature.
The shrinker replays from a clean worker state for every candidate.

### 11.3 Coverage guidance

Coverage guidance is a later optimization, not a prerequisite for deterministic replay.
Use a maintained coverage mechanism capable of arcs/branches, or Python monitoring where
it provides equivalent data. Plain `sys.settrace` line hits are not branch-edge coverage.

Normalize coverage to stable `(module, source-location or arc)` identities and exclude
the harness, generated code, GUI, and third-party dependencies. Corpus writes occur in a
single coordinator process using content-addressed trace files and atomic replacement;
workers never concurrently mutate a shared index.

Retain a trace when it adds normalized coverage, exercises a new semantic feature, or
produces a new invariant failure signature.

---

## 12. Phased Roadmap and Exit Criteria

Timings are estimates, not commitments. Each phase has an independently useful exit
criterion.

### Phase 0: determinism inventory and composition seams

- Inventory clock, UUID, random, filesystem, thread, process, and global singleton use.
- Add runtime parameters for bus, providers, workspace persistence, snapshot persistence,
  and optional services.
- Add a reliable runtime reset/teardown path.
- Define canonical observation and environment descriptor schemas.

Exit: two clean worker processes can execute the same hand-authored real-engine trace and
produce identical canonical observations.

#### Phase 0 outcome (completed 2026-10-15)

- Added the determinism inventory at `tech-ref/determinism-inventory.md`.
- Added runtime provider contracts and system defaults in `lib_runtime/providers.py`.
  Provider migration and seeded provider implementations remain Phase 1 work.
- Made `lib_runtime.app_host` the canonical composition root and retained
  `lib_runtime.runtime_factory.create_runtime` as a backward-compatible delegate.
- Added keyword-only injection for the bus, runtime providers, workspace adapter,
  snapshot adapter, optional services, solver enablement, and persistence mode.
- Added `InMemorySnapshotAdapter` and used it with `InMemoryJsonAdapter` in the Phase 0
  clean-process probe.
- Added idempotent context close plus explicit process-global reset through
  `lib_runtime.lifecycle.reset_runtime_state()`.
- Added versioned trace, environment, observation-spec, and canonical-observation schemas
  under `lib_dst`.
- Added a fixed hand-authored real-engine probe. It executes through `CommandSession` but
  intentionally contains no generated workloads, deterministic PRNG, oracle, mutation,
  virtual scheduling, or coverage logic.

Decisions:

- Canonical observations are opt-in projections named by logical dimension and cube
  names. Generated model, session, and message IDs are excluded.
- A custom bus is installed process-wide because legacy command paths still call
  `get_message_bus()`. Installing a different bus into a live runtime is rejected; the
  caller must reset the runtime first. Removing global bus access remains Phase 3 work.
- Injected workspace adapters are used directly. Production defaults retain the existing
  timeline-aware JSON adapter.
- A supplied `RuntimeServices` object is authoritative. Supplying it together with a
  separate snapshot adapter is rejected as ambiguous.
- Runtime context `close()` releases context-owned subscriptions, session state, and the
  engine. Resetting command-layer singletons is a separate explicit process operation.
- The command registry remains populated across resets because it contains application
  definitions rather than scenario state.

Discoveries:

- The runtime factory and application host duplicated nearly identical composition code;
  Phase 0 consolidated that ownership in the application host.
- Runtime adapter injection alone is insufficient for a custom bus because executor and
  publisher paths still contain global lookups. Process-wide installation is the Phase 0
  compatibility bridge.
- Additional singleton state exists in configuration, recording, help, profiling, and GUI
  modules. It is outside the Phase 0 probe and must join lifecycle ownership when those
  features enter DST scope.
- The repository's `pytest.ini` collects the top-level `tests/` directory. Nested package
  test trees require explicit invocation and were not implied by the Phase 0 validation.

Validation performed:

- `python -m pytest tests/test_dst_phase0.py -q`: 7 passed.
- `python -m pytest tests -q`: 15 passed.
- `python -m compileall -q lib_runtime lib_command lib_storeadapters lib_dst`: passed.
- A compatibility smoke run constructed the default solver service, executed a real
  command, and reset the runtime successfully with injected in-memory persistence.
- Direct Phase 0 probe runs with `PYTHONHASHSEED=101` and `PYTHONHASHSEED=202` produced
  byte-identical canonical JSON observations.

### Phase 1: real-engine deterministic core

- Implement key-derived PRNGs and worker-process isolation.
- Build typed symbol table and OM Core grammar generator.
- Execute real operations through `CommandSession` where available.
- Force serial recalculation and stable dependency ordering.
- Implement independent numeric oracle and error contracts.
- Implement trace serialization and exact replay.

Exit: at least 10,000 supported-subset seeds replay deterministically across repeated
runs on the same supported environment.

#### Phase 1 outcome (completed 2026-10-15)

- Added SHA-256 key derivation and independent generator/runtime streams in
  `lib_dst/seeding.py`. Every sub-seed used by the harness is recorded in trace metadata.
- Added deterministic clock, ID, and random providers. Model IDs use a context-scoped
  compatibility bridge in `lib_utils.ids`; session metadata and the local command
  request/executor/reply spine use providers carried by `ExecutionContext`. Callers that
  do not inject providers retain the existing system UUID and clock behavior.
- Added a typed logical symbol table in `lib_dst/symbols.py`. Serialized traces contain
  handles and logical names; worker-local runtime IDs never enter operations or
  observations.
- Added a harness-owned typed AST, OM Core renderer, and independent exact-arithmetic
  oracle in `lib_dst/ast.py` and `lib_dst/oracle.py`. The Phase 1 grammar covers finite
  integer literals, numeric cell references, unary negation, `+`, `-`, `*`, guarded `/`,
  comparisons, `IF`, scalar `SUM`, scalar `AVG`, and `ROUND`. A separate error feature
  deliberately exercises division by zero and requires `#DIV/0!`.
- Added deterministic seed-to-trace generation in `lib_dst/generator.py`. Rendered
  expressions pass through the production tokenizer/parser as a syntax gate, while the
  harness AST and oracle remain independent of the production evaluator.
- Added exact trace replay in `lib_dst/executor.py`. It creates schema, values, and rules
  through `CommandSession`, explicitly disables multithreaded recomputation, performs a
  serial full recalculation, observes logical state, and checks the oracle contract.
- Added batch worker isolation and a subprocess coordinator in `lib_dst/worker.py` and
  `lib_dst/runner.py`. Each scenario receives a fresh workspace/runtime and the explicit
  lifecycle reset; a worker process may execute a batch before being recycled.
- Stabilized dependency graph enumeration, dirty selection, topological traversal, and
  serial ready/frontier queues by node key.
- Added canonical execution reports containing the trace hash, observation, expected and
  actual oracle values, and pass status.

Decisions:

- The initial generated schema is intentionally fixed: one `Line` dimension with
  `InputA`, `InputB`, and `Result`, plus one `Calc` cube. Phase 1 varies input values and
  expression topology. Broader schemas and graph-specific scenario modes can be added
  without changing the trace executor contract.
- Seed domains use namespaced identifiers such as `workload.values`,
  `workload.expression`, and `runtime.ids`. The trace records the derivation inputs and
  identifies the generator as `phase1.numeric.v1`.
- Worker isolation is one process per deterministic seed batch, with a complete runtime
  teardown between seeds. The coordinator fixes `PYTHONHASHSEED` and `TZ` before process
  start. It does not attempt to change either inside a running interpreter.
- Replay deserializes the recorded trace and does not consult a workload PRNG. The
  expected oracle value and harness AST are materialized in trace metadata.
- Oracle arithmetic uses `fractions.Fraction`; finite engine results use
  `rel_tol=1e-9` and `abs_tol=1e-12`, while error codes are exact.
- Runtime IDs and envelope metadata are deterministic on the tested local command path
  but remain excluded from semantic observations. The context-scoped ID bridge preserves
  compatibility with model factories that currently call `lib_utils.ids.new_id()`.
- Volatile formula functions, UDFs, ranges, strings, hierarchy navigation, remote
  execution, solver execution, and multithreaded calculation are explicitly unsupported
  in Phase 1. Their remaining direct system-provider call sites are not claimed to be
  deterministic.

Discoveries:

- The actual rule command accepts logical `(dimension name, item name)` targets and
  cube-qualified expressions such as `Calc::[Line.InputA]`; this lets traces avoid view
  construction and runtime IDs while still using the public command surface.
- `set_rule` evaluates the anchored target immediately. The trace nevertheless includes
  an explicit full recalculation so the tested lifecycle and dependency ordering are
  unambiguous.
- OM Core workspaces bootstrap system dimensions and cubes. Canonical observation must
  remain an opt-in projection or those implementation-owned objects would dominate every
  trace.
- Dependency graph sets reached both public graph accessors and engine scheduling queues;
  sorting only `dirty_keys()` would not have made the serial frontier deterministic.
- The production parser types are private. Phase 1 imports them only as a validation gate;
  no production AST or evaluation helper is shared with the oracle.
- The synchronous local bus still uses a real blocking request primitive. Phase 1 relies
  on immediate in-process replies and does not claim virtual timeout/scheduling support;
  that remains Phase 3.

Validation performed:

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase1.py -q`: 39 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 54 passed.
- `.venv/Scripts/python.exe -m compileall -q lib_dst lib_runtime lib_command lib_utils lib_openm`:
  passed.
- `git diff --check`: passed (Git reported only existing line-ending conversion warnings).
- Two clean worker runs of seeds `0..9999`, with `PYTHONHASHSEED=424242` and `TZ=UTC`,
  produced the identical aggregate trace-plus-report SHA-256
  `ca0144183335fd945ae3bcd4b2a800decae1d8d112c683e9097eff8d16ed56bd` and zero
  oracle failures.

### Phase 2: persistence and convergence

- Inject `InMemoryJsonAdapter` and in-memory snapshot storage.
- Implement fresh-engine convergence and cache/idempotence invariants.
- Add persistence fault decorators.
- Implement failure signatures and structured shrinking.

Exit: every induced failure produces a standalone minimized trace that reproduces from a
clean process.

#### Phase 2 outcome (completed 2026-10-28)

- Refactored `lib_dst.executor` into reusable runtime construction, operation replay, and
  report projection functions while preserving the Phase 1 `execute_trace(trace)` API.
- Expanded canonical observations to include logical rule definitions and semantic rule
  precedence without exposing generated rule or model IDs.
- Added `lib_dst/invariants.py` with four explicit checks: first full-recalculation cache
  stability, second full-recalculation idempotence, in-memory snapshot round-trip, and
  JSON persistence convergence on a genuinely fresh engine.
- Added trace-recorded persistence fault decisions and a workspace-adapter decorator in
  `lib_dst/faults.py`. Supported outcomes are an exception on the recorded Nth save/load
  and malformed JSON on the recorded Nth load.
- Added stable failure signatures in `lib_dst/failures.py`. Identity consists only of
  category, lifecycle stage, and normalized code; raw paths, UUIDs, exception messages,
  and stack traces are excluded.
- Added dependency-aware validation in `lib_dst/validation.py`. It validates symbol
  creation order, observation targets, AST/rule agreement, referenced hard values,
  parser acceptance, serial mode, oracle presence, and unique fault occurrences before
  execution or shrinking.
- Added structured shrinking in `lib_dst/shrinker.py`. It attempts operation-range delta
  reduction, AST simplification, numeric reduction toward `0`, `1`, and `-1`, then fault
  removal. A candidate is retained only when it is smaller, structurally valid, and
  reproduces the same normalized signature after a complete runtime teardown/rebuild.
- Added stdin-based standalone trace reproduction to the existing worker/coordinator.
  Canonical trace JSON is the only input needed to reproduce a minimized failure in a
  clean process.
- Added a narrow payload replacement hook to `InMemoryJsonAdapter` so malformed content
  is exercised through the canonical JSON deserializer rather than simulated after load.

Decisions:

- Runtime A executes the trace, establishes the incremental observation, runs two full
  recalculations, takes/restores a snapshot, and saves through `CommandSession`. Runtime A
  is then closed and all local runtime singletons are reset before the adapter is read.
- Runtime B is constructed around the newly deserialized workspace. It has a new engine,
  empty caches, and a new dependency graph; it explicitly disables multithreaded
  recalculation, bootstraps dependencies, and performs a full recalculation. Calling
  `replace_workspace` on Runtime A is not accepted as cold-engine evidence.
- Snapshot round-trip and JSON cold convergence are separate invariants. Snapshot storage
  verifies the injected `InMemorySnapshotAdapter`; `InMemoryJsonAdapter` verifies the same
  persistence representation used by the file adapter.
- A fault decision records its layer, operation, occurrence, outcome, delivery guarantee,
  expected recovery, and whether state loss is allowed. Phase 2 implements only
  persistence `raise` and `malformed_json` outcomes. Request timeouts and delivery
  perturbations remain Phase 3 work.
- Save faults travel through the synchronous command bus as failed `ExecutionResult`
  values. A stable `DSTFAULT[code]` marker preserves the declared fault identity across
  that transport boundary without using raw error text as a signature.
- The current generated model has no independent schema object or reducible observation
  subset. Those shrink passes therefore reject invalid removals through structural
  validation; the shrink order remains ready for broader generated schemas.
- “Standalone minimized trace” means canonical schema-version-1 trace JSON reproduces the
  same normalized signature in a clean worker process. Corpus ownership, coverage, and
  mutation scheduling remain Phase 4.

Discoveries:

- The command-layer load path replaces an existing engine workspace. Cold convergence
  therefore has to load from the adapter outside that command and compose a second
  runtime; otherwise the test would only exercise derived-state reset on one engine.
- `InMemoryJsonAdapter` is memory-resident at its port boundary but deliberately delegates
  to the canonical file serializer through temporary files. It consequently exercises
  production migrations and bootstrap behavior without using a scenario-owned path.
- Engine recalculation retains or rebuilds calculated cube values depending on cache and
  dependency state. Comparing canonical observations after two recalculations catches
  both stale-cache and non-idempotent-result defects.
- Persistence exceptions raised inside command handlers are converted to command results
  by the command service. Failure normalization must therefore handle both live typed
  exceptions on direct adapter load and stable error codes transported as command text.
- AST reference dependencies are not fully represented by operation ordering alone.
  Shrinking required validation derived from the serialized harness AST to prevent
  deleting a hard value still needed by the oracle or rendered rule.
- Persisted rule IDs differ only incidentally; logical targets, expressions, anchored
  status, and precedence remain stable across JSON load and fresh-engine calculation.

Validation performed:

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase2.py -q`: 15 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase0.py tests/test_dst_phase1.py tests/test_dst_phase2.py -q`:
  61 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 69 passed.
- A 256-seed Phase 2 sweep passed all oracle, cache, snapshot, and fresh-engine checks and
  produced aggregate report SHA-256
  `08ca8c94a67ec64e4680c3b4404c171e13f6c236b44e0d8a2fa9cbd830c81d0b`.
- Save exception, load exception, and malformed-JSON failures each shrank to a smaller
  canonical trace and reproduced the same signature after JSON round-trip and in a clean
  subprocess with `PYTHONHASHSEED=202602` and `TZ=UTC`.
- `.venv/Scripts/python.exe -m compileall -q lib_dst lib_storeadapters`: passed.
- `git diff --check`: passed with only line-ending conversion warnings.

### Phase 3: virtual scheduling

- Remove direct global-bus access from the DST execution path.
- Implement scheduler-aware request/reply with virtual deadlines.
- Record causal event relationships and deterministic equal-time ordering.
- Add only contract-valid delay, timeout, and reorder scenarios.

Exit: scheduler scenarios complete without wall-clock sleeps or `threading.Event` waits,
and replay preserves the event order exactly.

#### Phase 3 outcome (completed 2026-11-04)

- Added `lib_runtime/virtual_scheduler.py`, a single-threaded integer-nanosecond scheduler
  implementing `call_at`, `run_next`, and `run_until`. Queue ordering is exactly
  `(logical_time_ns, insertion_sequence, event_id)`.
- Added `ScheduledMessageBus` in `lib_command/core/scheduled_message_bus.py`. Publications
  are queued on the virtual scheduler, and request/reply pumps scheduled callbacks until
  its correlated reply or virtual deadline. The scheduled request path contains no
  `threading.Event`, wall-clock sleep, or background thread.
- Added optional explicit-bus composition through runtime context, executor, gateway,
  command service, query service, and the command handlers used by DST. Production
  defaults still install and resolve the singleton bus for compatibility; scheduled DST
  construction opts out of global installation.
- Added request causation to command/query lifecycle and reply envelopes. The scheduled
  bus also records the currently delivered parent for engine-originated publications
  whose production envelopes do not carry an explicit causation ID.
- Added trace-recorded scheduler scenarios in `lib_dst/scheduling.py`. Decisions specify
  topic pattern, occurrence, delay in integer nanoseconds, mode, and delivery contract.
  Scenario generation uses the independent `scheduler` seed domain and materializes all
  decisions plus that sub-seed in trace metadata.
- Added canonical scheduled execution reports containing schedule order, delivery order,
  scheduled/delivered virtual time, normalized topic, stable message ordinal, causal
  parent, request deadline/outcome, pending delivery, and final semantic observation.
- Added clean-worker scheduled trace replay through stdin in the existing worker and
  coordinator. Dynamic command/query reply topics are normalized so session and
  correlation IDs do not become replay identities.
- Added explicit independent notification probes for serialized reorder scenarios and a
  non-mutating `workspace_summary` query probe for virtual timeout scenarios.

Decisions:

- The normal `MessageBus` remains synchronous and unchanged. Virtual delivery is selected
  only by composing `ScheduledMessageBus`; it is not a process-global mode switch.
- Existing transport timeout arguments remain seconds for API compatibility. The
  scheduled bus converts them once to integer nanoseconds and performs no wall-clock
  reads while waiting.
- Trace schema remains version 1. Scheduler policy is optional metadata, so Phase 0-2
  traces retain their existing wire format and execution behavior.
- Command requests and replies may be delayed within their deadline. Timeout injection is
  restricted to a non-mutating query request whose delivery is recorded after the
  deadline. Arbitrary mutation loss, retry, and exactly-once claims remain unsupported.
- Reorder decisions are accepted only for explicitly declared independent `event.*`
  notifications. Command requests, mutation commands, lifecycle messages, and replies
  cannot use the reorder contract.
- A timed-out request advances virtual time exactly to its deadline and remains pending.
  The scenario does not drain it afterward, preventing work from being silently applied
  after the caller has observed timeout.
- Equal-time events retain insertion order. Deliberate reordering is expressed by
  different recorded virtual delivery times, never by unstable heap/set ordering.
- Generated message, session, and correlation IDs are excluded from scheduler comparison.
  They are replaced by stable message ordinals; causal edges refer to those ordinals.
- Coverage monitoring, corpus selection, mutation weighting, and content-addressed corpus
  writes remain Phase 4 work.

Discoveries:

- Making the bus injectable at the composition root was insufficient by itself. The
  executor, gateway singleton constructor, system lifecycle publishers, query replies,
  and dimension-item adapter each contained a direct singleton lookup on the supported
  trace path.
- The command service previously forwarded an absent request causation ID into command
  lifecycle events. Treating the request message as their direct cause also gives command
  replies a stable structural parent.
- Query replies used standard-library UUID and performance-counter calls even though the
  command path had provider routing. Scheduled query replay required bringing query
  envelope metadata onto the same injected providers.
- Scheduling a publication while a request handler is running queues it rather than
  recursively delivering it. This exposes lifecycle and engine notifications as explicit
  equal-time events while preserving request-handler-before-reply causality.
- Engine event envelopes do not always carry command causation. Capturing the currently
  delivered scheduled message as an inferred parent records the actual nesting without
  changing production engine event DTOs.
- Command and query reply topics embed generated session/correlation IDs. Exact replay
  requires canonical topic families (`reply.command` and `reply.query`) rather than raw
  topic strings.

Validation performed:

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase3.py -q`: 12 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase0.py tests/test_dst_phase1.py tests/test_dst_phase2.py tests/test_dst_phase3.py -q`:
  73 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 81 passed.
- A 384-scenario sweep (128 seeds across delay, independent reorder, and timeout modes)
  passed all oracle, deadline, and causality checks twice and produced the identical
  aggregate scheduled-report SHA-256
  `45e578128e7fc07d4c4b6bfec800f5e2945846082df9b2e851c9207f6b312d24`.
- Delay, reorder, and timeout traces each produced byte-identical scheduled reports in
  repeated clean workers with fixed `PYTHONHASHSEED` and `TZ=UTC`.
- A test replaces `threading.Event` with a failing sentinel while a full real-engine trace
  executes successfully through the scheduled request path.
- `.venv/Scripts/python.exe -m compileall -q lib_command lib_runtime lib_dst`: passed.
- `git diff --check`: passed with only line-ending conversion warnings.

### Phase 3.5: OpenM script export and visual replay

- Add a deterministic exporter from a validated `TraceTranscript` to a standalone
  `.openm` script for the supported real-engine subset. The export must include the
  trace hash and master seed in comments and preserve trace operation order.
- Reconstruct rule expressions from the serialized harness AST with `lib_dst.ast.render`
  and require them to agree with the recorded `set_rule` expression before export. Pass
  the result through the production expression parser as an export-time validation gate.
- Translate supported operations to logical-name-based script commands: serial runtime
  configuration, dimension and item creation, cube creation, hard values, rules, and full
  recalculation. Unsupported operations must fail export explicitly rather than being
  omitted.
- Repair or provide a supported script spelling for anchored logical rules. The current
  trace subset records `is_anchored=True`, while the existing `$` target handling resolves
  the cube before stripping the anchor marker and therefore is not yet a reliable export
  target.
- Append a clearly marked presentation-only view and expected-result annotation so the
  resulting model can be inspected in the GUI. Add a documented GUI import/source path;
  if the GUI has no general script-import action, expose the existing script runner through
  a narrow "Import OpenM Script" action rather than treating a script as a persisted model.
- Keep the canonical JSON trace as the replay authority. The `.openm` export is a semantic
  reproduction and human inspection artifact: generated runtime IDs and timestamps may
  differ, and persistence-fault decisions, virtual scheduler delays/timeouts/reordering,
  and other harness-only metadata are emitted as comments but are not simulated by the
  GUI script.
- Test byte-identical export, AST/expression agreement, explicit rejection of unsupported
  operations, and fresh-runtime script execution. Compare the script-created workspace's
  canonical observation and oracle value with execution of the source trace. Include
  ordinary numeric, `CellError`, minimized-failure, and GUI-import smoke cases.

Exit: every structurally valid trace in the supported semantic subset exports to a
standalone `.openm` file that executes from a fresh workspace and produces the same
canonical model result as trace replay, and that file can be loaded through a documented
GUI workflow for visual inspection. Harness-only fault and scheduling behavior remains
replayable only from canonical trace JSON and is identified as such in the export.

#### Phase 3.5 outcome (completed 2026-11-04)

- Added `lib_dst/script_export.py` with deterministic text and file export APIs. Exports
  include the canonical trace hash, master seed, one ordered command per trace operation,
  the reconstructed AST expression, oracle guidance, and a trace-hash-named presentation
  view.
- Export validation first applies normal trace validation, reconstructs the expression
  from the harness AST, checks exact agreement with the recorded rule, and passes the
  result through the production expression parser. Unsupported identifiers, values,
  operations, scopes, and ambiguous rule sets fail with `ScriptExportError`.
- Added logical script spellings for individual dimension-item creation and fixed cube
  creation to resolve dimension names to runtime IDs. Fixed `$Cube::...` rule targets by
  removing the anchor marker before cube-name resolution, so exported rules retain the
  trace's anchored semantics.
- Added `lib_scripting/script_import.py`, which sources a script through the existing
  `OpenMREPL` parser and returns a structured result without introducing a second script
  interpreter. `source` now records its last outcome while preserving its existing falsey
  return contract.
- Added **File -> Import OpenM Script...** to the GUI. It imports into the current session,
  uses existing refresh suspension/coalescing, refreshes the read models afterward, and
  marks the workspace dirty.
- Added `--export-script-stdin [PATH]` to `lib_dst.worker`. With no path it writes the
  script to stdout; with a path it writes UTF-8 directly, avoiding shell encoding and BOM
  differences. Added the corresponding GUI and worker workflow to `README.md`.

Decisions:

- Canonical trace JSON remains the only replay authority. Persistence fault and scheduler
  policies are included as deterministic comments but are not converted into script
  commands or claimed to run in the GUI.
- Phase 3.5 exports the current single-rule, AST-backed semantic subset. Names must be
  unquoted OpenM identifiers and hard values must be finite numeric literals. Broader
  string/name grammar and multiple independent ASTs require explicit future schema
  support rather than lossy escaping or rule selection.
- Each trace operation maps to a distinct script command. Dimension items are not folded
  into dimension creation, preserving the trace's visible operation order and making
  manual comparison straightforward.
- The presentation view is appended only after all trace operations and is named from the
  trace hash. Views are outside the canonical DST observation, so it cannot change the
  semantic comparison.
- Numeric script checks use lower and upper bound assertions matching the oracle's
  `rel_tol=1e-9` and `abs_tol=1e-12` contract. Error expectations are emitted as explicit
  visual annotations because the current script assertion language cannot compare
  `CellError` values.
- GUI import is additive and never discards or replaces a workspace. The documented
  workflow asks users to start from a fresh workspace so logical-name collisions cannot
  alter the intended spot-check model.

Discoveries:

- The documented cube script syntax accepted dimension names, but `do_cube` forwarded
  them to the canonical command as IDs. Export round-trip required resolving logical names
  at the scripting boundary.
- The rule parser tested for `$` anchoring only after attempting cube resolution, causing
  it to search for a literal cube named `$Calc`. Moving anchor parsing ahead of resolution
  makes the existing intended syntax usable.
- There was no script command for one logical-name-addressed dimension-item operation.
  The prior source optimization accepted ID-addressed `exec` records, which cannot form a
  portable human-readable trace artifact.
- Successful script assertions printed a Unicode checkmark that can raise a Windows
  console encoding error and be misreported as a failed assertion. The success message is
  now ASCII, and `source` records truthy command/assertion failures in its structured
  result.
- Exact script equality is not a valid substitute for the harness oracle: seed 18 exposed
  a raw binary floating-point value that is canonically equal within tolerance. Exported
  assertions therefore use the oracle's tolerance rather than exact equality.
- The combined GUI and REPL already share a command session. A narrow GUI action can reuse
  the production source parser and batch behavior without teaching the GUI a second file
  format or treating scripts as persisted workspace JSON.

Validation performed:

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase35.py -q`: 15 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase0.py tests/test_dst_phase1.py tests/test_dst_phase2.py tests/test_dst_phase3.py tests/test_dst_phase35.py -q`:
  88 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 96 passed.
- A 128-seed export -> fresh-runtime import sweep, including four `CellError` traces,
  produced observations and oracle results identical to direct trace execution. Repeated
  export produced the identical aggregate script SHA-256
  `76d4b4eafe6184b8a4ae8b72949a8c5a0fe5426336e7420f4e261b692ad2f838`.
- Worker stdout and direct UTF-8 file export are byte-identical to the library exporter.
- `.venv/Scripts/python.exe -m compileall -q lib_dst lib_scripting lib_gui`: passed.
- `git diff --check`: passed with only line-ending conversion warnings.

### Phase 4: coverage-guided evolution

- Add normalized branch/arc coverage.
- Build coordinator-owned, content-addressed corpus storage.
- Add type-preserving trace and AST mutation.
- Weight corpus selection by rarity, age, size, and semantic feature coverage.

Exit: corpus evolution discovers and minimizes seeded defects and remains deterministic
when mutation decisions are replayed.

#### Phase 4 outcome (completed 2026-11-04)

- Added `lib_dst/coverage.py`, using maintained `sys.monitoring` branch events rather than
  line tracing or an added coverage dependency. Arc identities contain repository-relative
  module path, qualified code name, source-line transition, and branch side. Collection is
  restricted to `lib_openm`, `lib_command`, `lib_runtime`, and `lib_storeadapters`.
- Added `lib_dst/mutation.py` with versioned, materialized mutation decisions. Supported
  mutations cover hard values, AST literals, numeric operators, compatible cell
  references, recalculation insertion/removal, valid scheduler delays, and existing
  persistence fault decisions. Every decision records its parent trace hash, independently
  derived mutation seed, index, kind, target, precondition, and replacement.
- Mutation application consumes no PRNG, verifies its parent and precondition, refreshes
  rendered rules and the independent oracle where necessary, validates the resulting
  trace, and records provenance in optional schema-version-1 metadata.
- Added `lib_dst/corpus.py`, a coordinator-owned content-addressed corpus. Trace filenames
  are their canonical SHA-256 values; index and failure-manifest writes use same-directory
  temporary files, flush/fsync, and atomic replacement. Workers never receive a corpus
  path.
- Corpus retention accepts a candidate only for new normalized arcs, new semantic
  features, or a new normalized failure signature. Selection uses deterministic integer
  weights combining arc rarity, semantic-feature rarity, generations since last selection,
  and inverse trace size.
- Added `lib_dst/evolution.py` and a clean-worker candidate mode. The coordinator selects a
  parent, records and applies one mutation, obtains coverage and failure evidence from an
  isolated worker, performs all corpus writes itself, and invokes the existing structured
  shrinker for each newly discovered failure signature.

Decisions:

- The trace schema remains version 1. Mutation records are optional metadata, matching the
  compatibility strategy used for fault and scheduler policy.
- Coverage is production semantic evidence, not profiler evidence. Harness, GUI, tests,
  third-party files, generated bytecode, and wall-clock diagnostic branches are excluded.
- Corpus age is a deterministic generation count. No wall-clock creation/access time,
  random temporary filename, absolute corpus path, or worker PID is part of selection or
  report identity.
- Workers return immutable coverage/outcome JSON only. The process that constructs
  `CorpusStore` is enforced as its sole writer; worker concurrency cannot race on an index.
- Mutation selection uses the independent `mutation.<index>` seed domain. Corpus parent
  selection uses a separate key-derived `mutation.selection.<generation>` domain.
- Scheduler and persistence mutations change only already recorded, contract-valid
  decisions. Phase 4 does not add mutation loss, duplicate commands, invalid reordering,
  or a new retry/idempotency contract.
- The existing declared persistence-fault mechanism is used as the seeded failure proving
  discovery, normalized signature retention, and minimization. No defect is inserted into
  production engine code for the test.
- Phase 4 evolves the existing small semantic trace shape. Large-model stress profiles,
  throughput budgets, and dependency/persistence/event-volume scaling remain a separate
  post-Phase-4 step. Phase 5 CLI and CI work has not begun.

Discoveries:

- Branch monitoring adds enough evaluator overhead that raw wall-clock profiling can cross
  OM Core's `SLOW_LOG_THRESHOLD`. That initially produced three different arcs across
  otherwise identical clean campaigns. Excluding the diagnostic profiling function and
  threshold/debug branches restored exact coverage identity while preserving surrounding
  rule-evaluation branches.
- A compatible AST reference cannot be selected only from references already present in
  the expression: many generated expressions contain one input reference while the symbol
  table contains two valid hard-valued inputs. Reference mutation therefore selects from
  typed hard-value symbols and replaces the full reference node, including its logical
  name fields.
- The current supported trace has few dependency-safe structural edits. Full
  recalculation is the safe command insertion/removal target; schema-object mutation needs
  the planned broader workload topology rather than invalidating symbol dependencies.
- One Phase 2 convergence candidate exercises roughly 1,400 normalized production arcs.
  Storing per-entry sorted arcs makes rarity calculations and corpus replay transparent at
  the cost of a deliberately verbose coordinator index.

Validation performed:

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase4.py -q`: 15 passed.
- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase0.py tests/test_dst_phase1.py tests/test_dst_phase2.py tests/test_dst_phase3.py tests/test_dst_phase35.py tests/test_dst_phase4.py -q`:
  103 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 111 passed.
- A repeated clean-directory campaign with three initial seeds and eight evolution steps
  produced byte-identical reports and indexes. Report SHA-256 was
  `e196d6927d8541d54a26f3a9cf19d90e9b7f2b04f826afbf7a5e71bbb9ba0a96`; corpus-index
  SHA-256 was `68720f0a8445b654d26e32a038434f61f9a035b05f5942b2fd5f2495c4fa3b2a`.
- A 256-candidate mutation/replay sweep produced exact decision replays, valid traces, and
  passing real-engine oracles for every candidate. Aggregate mutated-trace SHA-256 was
  `2c7c1ed7369fb6a1a9e5a2e6f0733dff7e20b44ef3737e92176dfdba0bf9bc9c`.
- Scheduler-delay and persistence-fault mutation replay are covered explicitly. A seeded
  persistence failure was discovered, shrunk to a smaller standalone trace, and retained
  under its normalized signature.
- `.venv/Scripts/python.exe -m compileall -q lib_dst`: passed.
- `git diff --check`: passed with only line-ending conversion warnings.

### Phase 4.5: deterministic scale and stress profiles

- Replace the single fixed `Line(InputA, InputB, Result)` workload shape with a
  parameterized, typed topology generator. Generated traces may contain multiple
  dimensions, items, cubes, hard-value cells, calculated cells, and rules while retaining
  logical handles and dependency-valid operation ordering.
- Define serialized workload profiles rather than interpreting a profile name at replay
  time. A trace records its resolved limits and all generated topology so replay never
  depends on later profile-default changes:
  - `small`: fast semantic regression cases suitable for eventual pull-request use;
  - `medium`: multiple cubes, dimensions, and rule chains for scheduled campaigns;
  - `large`: bounded high-cardinality models for nightly or dedicated workers;
  - `dependency_deep`: long acyclic chains, broad fan-in/fan-out, incremental edits, and
    large dirty frontiers;
  - `persistence_heavy`: large snapshots and repeated save/load/fresh-engine convergence;
  - `event_heavy`: high command and notification volume under the virtual scheduler.
- Add a bounded `stress_default` campaign preset composed of independent stress-axis
  runs rather than one model that maximizes every property simultaneously. Phase 4.5
  exposes the serialized preset through its library API; Phase 5 may provide the
  convenient public `--stress-default` alias. The preset exercises calibrated, heavy but
  non-destructive values for expression depth and width, UDF depth, cube and rule count,
  dimension count and cardinality, hierarchy depth and breadth, dependency depth and
  fan-in/fan-out, populated-cell density, aggregation range, recalculation churn,
  persistence volume, command/event volume, model churn, and stored view/format volume.
- Add deterministic single-axis limit discovery, with a future Phase 5 interface such as
  `--stress-axis <axis> --discover-limit`. Start from a recorded lower bound, grow the
  requested value geometrically until the first failure or configured safety ceiling,
  then refine the passing/failing interval when the result is monotonic. Non-monotonic
  results retain every tested point and report an observed envelope rather than claiming
  an exact maximum. Each point is a standalone, replayable trace executed in a fresh
  worker so a crash cannot destroy the coordinator or the evidence from earlier points.
- Treat superficially similar depth limits as separate axes: textual rule parsing depth,
  serialized trace-AST depth, engine AST evaluation depth, built-in function nesting,
  UDF call-chain depth, and calculated-cell/cross-cube dependency depth. Deep-expression
  cases use a directly known or metamorphic result where necessary so the independent
  oracle does not encounter the same recursion limit before OM Core; an oracle or trace
  serialization failure is classified as a harness failure, not an engine limit.
- Make cube and rule limit discovery similarly explicit. Cube axes distinguish empty
  schema objects from populated and calculated cubes, shared from cube-specific
  dimensions, and independent cubes from cross-cube dependency chains. Rule axes
  distinguish total rules, rules per cube, anchored from masked rules, mask overlap and
  precedence, reference/AST breadth, and cross-cube edges. This keeps a reported limit
  attributable to the structure that produced it.
- Give every scale choice an independent key-derived seed domain, including topology,
  cardinality, rule graph, value density, operation mix, and observation sampling. Record
  resolved counts, density, estimated cells, rule edges, and operation count in trace
  metadata.
- Keep generation bounded by explicit structural budgets: maximum dimensions, items per
  dimension, addressable cells, populated cells, cubes, rules, AST nodes, dependency
  edges, operations, persistence payload bytes, and virtual events. Reject a generated
  candidate before execution if it exceeds its materialized budget.
- Generate only dependency-valid acyclic numeric graphs initially. Continue using the
  independent typed AST interpreter where its semantics are tractable; use cold-engine,
  cache/idempotence, persistence, and metamorphic invariants for larger observations that
  are intentionally sampled rather than fully enumerated.
- Extend canonical observation with deterministic sampling for large cubes. Samples are
  selected from a recorded seed domain and materialized as logical addresses in the trace;
  important boundaries, rule targets, edited precedents, errors, and persistence probes
  are always included.
- Extend mutation and shrinking to understand the broader topology. Mutators may change
  cardinality, density, graph shape, rules, and operation bursts only when symbol and type
  dependencies remain valid. Shrinking may reduce scale, but it retains a candidate only
  when the same normalized failure signature reproduces.
- Make corpus selection profile-aware. Maintain semantic and coverage novelty across
  profile/scale strata so numerous cheap `small` traces cannot starve rarer large,
  dependency-heavy, persistence-heavy, or event-heavy candidates.
- Separate deterministic correctness from performance measurement. Correctness outcomes,
  traces, coverage, and failure signatures must replay exactly. Wall-clock duration,
  throughput, peak memory, and persistence size are reported with the environment
  descriptor and used to calibrate budgets, not treated as cross-machine deterministic
  oracle values.
- Record every stress point with the requested and realized scale, trace identity, seed
  domains, resolved budgets, worker exit evidence, normalized exception or mismatch,
  volatile resource measurements, environment descriptor, and OM Core revision. A limit
  report contains all attempted points plus the last observed pass and first observed
  failure; it does not retain only the final boundary.
- Give stress outcomes a structured classification with separate verdict, origin, and
  kind fields so materially different breaks cannot collapse into a generic test failure:
  - `correctness_mismatch`: OM Core completed but disagreed with the independent oracle,
    convergence invariant, persistence invariant, or deterministic replay;
  - `controlled_engine_error`: OM Core rejected or failed a valid within-budget case with
    a caught, normalized exception such as `RecursionError`;
  - `worker_exception`: worker/runtime infrastructure raised outside the engine boundary;
  - `worker_crash`: the child terminated abnormally with crash evidence and no result;
  - `resource_exhaustion`: a memory, process, payload, or other resource limit was
    positively identified by an enforced limit or platform exit evidence;
  - `performance_boundary`: the point exceeded a configured measurement threshold while
    remaining a correctness pass; this is environment-specific calibration data;
  - `safety_ceiling`: the next point was deliberately not executed because it exceeded a
    configured structural or resource guard, making the boundary inconclusive;
  - `generation_rejected`: the requested model could not be materialized within its
    recorded structural budget and was never presented to OM Core;
  - `harness_error`: generation, oracle, trace serialization, coordinator, or validation
    failed independently of OM Core;
  - `unknown_abnormal_exit`: the available evidence cannot distinguish a crash, external
    kill, or resource exhaustion; preserve the raw evidence and never infer a cause.
- Normalize deterministic classification evidence into the failure signature used for
  replay and shrinking, while excluding wall-clock and peak-memory values. Outcomes that
  depend on host enforcement or an unexplained exit retain their environment descriptor
  and are reproducible observations, not portable deterministic limits. The coordinator
  must distinguish an expected invalid-input rejection from a valid-case engine failure.
- Measure real-engine throughput, memory, persistence payload size, and shrink cost for
  each profile before choosing default sizes. Profile limits remain configuration data
  derived from empirical runs rather than unverified targets such as a fixed million-seed
  campaign.

Decisions:

- Phase 4.5 targets the local Python engine in serial mode. Large-model testing does not
  authorize remote engine, solver, GUI event-loop, or multithreaded execution; each still
  requires its own deterministic boundary before stress coverage is claimed.
- `large` means bounded by a recorded structural/resource budget, not unbounded allocation
  or a wall-clock timeout. Out-of-budget generation is a rejected candidate, while an
  engine failure within budget is a reproducible harness outcome.
- `stress_default` is safe-by-policy and repeatable; it is not expected to crash or exhaust
  its host. Limit discovery is opt-in and still stops at configured Phase 4.5 ceilings.
  Phase 5 worker memory/time enforcement is required before campaigns intentionally probe
  host resource exhaustion beyond those ceilings.
- A discovered limit is always qualified by stress axis, resolved model shape, environment,
  and OM Core revision. It is not advertised as a universal maximum, and the harness does
  not use a wall-clock threshold as a deterministic correctness oracle.
- Sparse models are represented as materialized populated addresses rather than by
  enumerating their entire Cartesian cell space. Estimated addressable size and actual
  populated-cell count are recorded separately.
- Phase 4.5 supplies library APIs, trace metadata, generator profiles, corpus integration,
  and empirical calibration. Phase 5 remains responsible for the public CLI, PR/nightly
  scheduling, worker resource limits, and artifact publication.

Implementation decisions and discoveries (2026-11-10):

- `lib_dst.profiles` now owns immutable `StructuralBudgets`, resolved
  `WorkloadProfile` values, realized `WorkloadMetrics`, and the six named profile
  defaults. `lib_dst.scale_generator` materializes all topology, independent domain
  seeds, observations, oracle probes, resolved profile values, and realized metrics in
  each trace. The original `generate_trace(seed)` API and schema-1 traces remain valid.
- Observation specs gained optional logical cell samples. Absence still means full-cube
  observation for old traces; scale traces materialize their sampled addresses, always
  retaining oracle targets and input boundaries. Execution reports retain the original
  primary oracle fields and add optional per-probe results.
- Generated topology now covers multiple dimensions and items, single- and
  multidimensional cubes, anchored rules, cross-rule and cross-cube acyclic dependency
  chains, configurable fan-out, hierarchy outlines, stored views, transient cube churn,
  repeated recalculation, persistence cycles, and format/UDF operations on their
  dedicated stress axes. The six ordinary profiles all pass real-engine convergence.
- Phase 4.5 exposes `generate_profile_trace`, `generate_stress_axis_trace`,
  `run_stress_default`, `run_limit_discovery`, and `calibrate_profiles` as library APIs.
  The worker's hidden `--stress-point-stdin` switch is an internal subprocess protocol,
  not the Phase 5 public CLI. `stress_default` currently contains twenty independent
  axes: serialized AST depth, built-in nesting, expression width, UDF depth, cube count,
  rule count, dimension count, cube rank, dimension cardinality, hierarchy depth and
  breadth, dependency depth and fan-out, populated cells, persistence cycles, event
  volume, recalculation churn, view count, format volume, and model churn.
- Limit discovery records every attempted point, grows geometrically, refines a
  monotonic passing/failing bracket, resumes from prior reports, emits a safety-ceiling
  point when no failure is observed, and reports an envelope when retained evidence is
  non-monotonic. Report identity excludes volatile measurements and resume mechanics.
- Stress classification is a three-part verdict/origin/kind value. Clean child results,
  normalized failures, exit status, stderr tail, environment, and revision are retained
  separately. Memory/resource exhaustion is claimed only for `MemoryError`, an enforced
  limit, or a recognized platform status; unrecognized exits remain
  `unknown_abnormal_exit`. A completed correctness pass may separately be marked as an
  environment-specific `performance_boundary`.
- Scale traces have exact recorded mutations for cardinality, populated density, new
  rules, dependency graph edges, and recalculation bursts. Realized metrics are
  recomputed and revalidated after mutation. Profile-aware shrinking first regenerates a
  smaller valid topology and retains it only when the normalized failure signature is
  unchanged. Corpus entries record profile/scale strata, and stratum rarity contributes
  to deterministic selection. Evolution accepts `(profile, seed)` initializers.
- Calibration runs in clean workers and records elapsed time, traced peak memory, trace
  and report sizes, the actual serialized persistence payload, and a seeded shrink's
  duration/candidate count. Volatile values do not enter deterministic report identity.
- The dedicated UDF axis found that the registered `create_udf` command currently fails
  because the public `Engine` facade does not expose `udf_registry`. The dedicated format
  axis found that semantic `Dimension.Item` selectors reach the ID-addressed hard-value
  API without being resolved to stable item IDs. Both are preserved as replayable
  `controlled_engine_error` outcomes; the harness does not bypass the command boundary or
  patch OM Core internals. Ordinary profiles keep these two counts at zero until the
  production command surfaces are repaired.
- Phase 3.5 script export retains its established supported-operation contract. JSON
  remains the replay authority for expanded scale traces containing operations that the
  OpenM script language/exporter cannot yet represent faithfully.

Known command-boundary defects and reproduction:

1. UDF creation cannot cross the public `Engine` facade.

   Reproduce from the repository root in PowerShell:

   ```powershell
   .venv\Scripts\python.exe -c "import json; from lib_dst.scale_generator import generate_stress_axis_trace; from lib_dst.stress import run_stress_point_in_worker; t=generate_stress_axis_trace(4500, 'udf_depth', 1); r=run_stress_point_in_worker(t, axis='udf_depth', requested_value=1); print(json.dumps(r.to_dict(), sort_keys=True))"
   ```

   Current deterministic result:

   - verdict/origin/kind: `failed / engine / controlled_engine_error`;
   - normalized code: `RuntimeError`;
   - underlying command error: `Operation ... (create_udf) failed: 'Engine' object has no
     attribute 'udf_registry'`;
   - the generated trace and its SHA-256 remain available in the point result for replay.

   Cause and call path:

   - `lib_dst.executor.execute_operations` sends the registered `create_udf` command
     through `runtime.session.execute`;
   - `lib_command.commands.udf_commands.cmd_create_udf` calls
     `engine.udf_registry.register(...)`;
   - the runtime supplies the public `lib_openm.api.Engine` facade. Its `__getattr__`
     delegates only names beginning with `_` and therefore rejects `udf_registry`;
   - the registry exists on private `_EngineCore`, but reaching into `_core` from the
     harness or command would hide the missing public contract rather than repair it.

   Required production repair: expose facade-level UDF registration/query/deletion
   methods (preferred), or another explicit public UDF port, and make the command use that
   surface. Add a command-through-session test that creates, evaluates, persists, reloads,
   and deletes a UDF without accessing `_EngineCore` directly.

2. Formatting writes semantic selectors into an item-ID address API.

   Reproduce from the repository root in PowerShell:

   ```powershell
   .venv\Scripts\python.exe -c "import json; from lib_dst.scale_generator import generate_stress_axis_trace; from lib_dst.stress import run_stress_point_in_worker; t=generate_stress_axis_trace(4500, 'format_volume', 2); r=run_stress_point_in_worker(t, axis='format_volume', requested_value=2); print(json.dumps(r.to_dict(), sort_keys=True))"
   ```

   Current deterministic result:

   - verdict/origin/kind: `failed / engine / controlled_engine_error`;
   - normalized stage/code: `observation.address / invalid-item-key`;
   - the invalid key has the semantic form `ScaleDim000.Item0000` where canonical cube
     storage expects the stable item ID for that dimension;
   - the formatting command reports success, and canonical observation detects the mixed
     address representation afterward.

   Cause and call path:

   - the harness calls the registered `set_property` command with an address target and
     `format.bold`. An earlier harness draft called the obsolete bare `set` command; that
     harness-side command-ID error has been corrected and is not the production cause;
   - `cmd_set` correctly dispatches to `cmd_set_format`, which parses the semantic target;
   - `_set_format_at_address` splits the target into strings such as
     `ScaleDim000.Item0000` and passes those strings directly to
     `engine.set_cell_hardvalue_by_addr` with the technical format channel;
   - `set_cell_hardvalue_by_addr` normalizes channel placement but treats the remaining
     tuple members as item IDs. The semantic strings are consequently stored as cube keys;
   - `observe_workspace` now raises `ObservationAddressError` for this malformed state, so
     the harness reports a controlled engine-boundary failure instead of an ambiguous
     worker `KeyError`.

   Required production repair: resolve every semantic `Dimension.Item` selector to its
   stable item ID before invoking the ID-addressed API, reject missing/duplicate/wrong-
   dimension selectors, and do not report success when no cell was formatted. Add command,
   canonical-observation, save/load, and one-dimensional formatting tests. The current
   helper also returns no affected cell for fewer than two selectors, so one-dimensional
   cubes require explicit coverage in the repair.

Both defects can be checked together with:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dst_phase45.py -q -k "udf_and_format"
```

Validation:

- Repeated generation of every profile with the same master seed produces byte-identical
  traces; changing one profile seed domain does not perturb unrelated topology decisions.
- Representative traces from every profile execute against the real engine, satisfy their
  applicable oracle and convergence invariants, replay in clean workers, and remain within
  their recorded structural budgets.
- Mutation decisions for expanded schemas replay exactly, and seeded failures in deep,
  persistence-heavy, and event-heavy models shrink to standalone traces with the same
  normalized signature.
- Two bounded mixed-profile evolution campaigns with the same inputs produce identical
  selection decisions, corpus indexes, minimized failures, and canonical reports.
- Calibration reports record per-profile seed throughput, peak memory, payload size, and
  shrink cost without placing those volatile measurements in deterministic comparison
  identities.
- Repeated `stress_default` campaigns with the same inputs produce identical traces,
  stress-axis order, structural results, classifications, and report identity after
  volatile measurements are excluded.
- Limit-discovery tests cover geometric growth, monotonic refinement, non-monotonic
  envelopes, safety-ceiling termination, and resume from a partially completed report.
  Every attempted point remains independently replayable.
- Inject or seed each outcome class at its proper boundary and verify that correctness
  mismatches, controlled engine exceptions, worker exceptions, abnormal child exits,
  positively identified resource exhaustion, performance boundaries, generation
  rejection, harness failures, and ambiguous exits receive distinct classifications.
  Classification tests simulate destructive resource conditions where possible rather
  than deliberately exhausting the validation host.

Exit: deterministic campaigns exercise multiple bounded model sizes and targeted
dependency, persistence, and event-volume stress profiles against the real serial engine.
Every retained trace and minimized failure replays from a clean worker, mixed-profile
corpus evolution is deterministic, and measured resource data is sufficient for Phase 5
to assign small, scheduled, nightly, `stress_default`, and opt-in limit-discovery workloads
without changing trace semantics. Stress reports preserve every attempted point, attribute
known failures to the correct boundary, and explicitly leave ambiguous terminations
unclassified rather than reporting a misleading maximum.

Validation performed (2026-11-10):

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase45.py -q`: 27 passed.
- All Phase 0-4.5 DST modules together: 130 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 138 passed.
- `.venv/Scripts/python.exe -m compileall -q lib_dst`: passed.
- `git diff --check`: passed with only line-ending conversion warnings.
- Two complete `stress_default` campaigns with master seed `4500` produced identical
  deterministic identity SHA-256
  `6ced520d98186a3330a91508e4841eb1457db9333e83dd857d2c3c35e177d7bb`.
  Eighteen axes passed; UDF depth and format volume deterministically reproduced the two
  command-surface errors documented above.
- A six-profile calibration with master seed `4500` passed every profile. Observed
  serialized persistence payloads ranged from 23,937 to 198,942 bytes, traced peak memory
  from 2,432,275 to 6,968,100 bytes, and seeded shrink work from 3 to 6 candidates. These
  measurements describe this environment and are not portable limits.

### Phase 5: CI and scale

- Add `om-dst run`, `om-dst replay`, and `om-dst shrink` commands.
- Run a small fixed regression corpus on pull requests.
- Run bounded seed batches on scheduled CI workers.
- Publish failing traces, environment descriptors, and canonical diffs as artifacts.
- Add remote engine, solver, GUI, or multithreaded modes one at a time only after each has
  deterministic provider/scheduler boundaries.

Exit: CI runtime and seed throughput are measured empirically. Seed-count targets such as
one million runs are adopted only when observed throughput and infrastructure cost make
them credible.

#### Phase 5 outcome (implemented 2026-11-17)

- Added the public `om-dst` CLI in `lib_dst/cli.py`, run as `python -m lib_dst`
  (`lib_dst/__main__.py`) or through the root wrappers `om-dst` (POSIX sh) and
  `om-dst.ps1`. `[tool.uv] package = false` is unchanged. Subcommands:
  - `run --corpus DIR | --seeds START:COUNT [--profile P] | --stress-default |
    --stress-axis AXIS (--discover-limit [--ceiling N] | --value N)`;
  - `replay TRACE [--strict-env]`;
  - `shrink TRACE`.
  Common options: `--out`, `--overwrite`, `--resume` (run only), `--timeout`,
  `--memory-mb`, `--no-shrink`.
- Exit-code contract: 0 passed/matched expectations; 1 failure or `xpass`; 2 usage or
  invalid input (argparse errors, malformed trace, corrupt corpus, run-directory
  collision, shrinking a passing trace); 3 inconclusive; 4 `--strict-env` replay
  rejected before execution. stdout carries exactly one canonical JSON summary line;
  progress is on stderr.
- Added `lib_dst/artifacts.py`. A run directory is `<out>/<mode>-<sha256(command)[:16]>/`
  containing `manifest.json` (status `incomplete` until finalized, command,
  provenance), `summary.json` (deterministic identity plus `identity_sha256`),
  `measurements.json` (volatile only), `checkpoints/`, `failures/<trace-sha>/` and
  `stress/<axis>.json`. Every failure retains the original canonical `trace.json`
  (file SHA-256 equals the trace hash) plus `outcome.json`, `environment.json`,
  `diff.json` when an invariant disagreed, and `minimized.json`/`shrink.json` when a
  normalized signature exists. Writes use the atomic writer now shared with
  `CorpusStore`. A complete run is replaced only with `--overwrite`; an incomplete one
  continues only with `--resume` and the identical command; a directory without an
  `om-dst` manifest is never used or deleted.
- Added `lib_dst/provenance.py` (git revision with `-dirty` flag, tracked dependency
  versions, pinned worker environment, compatibility comparison) and
  `lib_dst/process.py` (`run_worker`, which never raises for an abnormal child and
  records which limits were enforced). Workers now receive explicit `cwd` and
  `PYTHONPATH`, so the CLI and the existing `lib_dst.runner` helpers work from any
  working directory.
- Added `lib_dst/evaluation.py` and hidden worker switches `--evaluate-stdin`,
  `--seed-outcomes`, and `--shrink-stdin`. Seed batches emit one JSON line per seed and
  flush after each, so an abnormal batch exit loses no completed seed; every unreported
  seed is re-run alone and attributed individually. Shrinking runs in a worker, and each
  minimized trace is replayed in a further clean worker before it is reported as
  reproduced. `execute_convergence`/`capture_failure` accept an optional `evidence`
  dict exposing the observation behind each invariant check; `lib_dst/diff.py` turns
  those into deterministic canonical diffs. Report schemas are unchanged.
- Added the fixed PR regression corpus `tests/dst_corpus/` (25 entries: four legacy
  seeds including `#DIV/0!`, all six profiles, three scheduler modes, three declared
  persistence faults, one dependency stress point, both known command-boundary defects,
  and six Phase 4 mutation-evolved traces copied from `.om/dst-corpus-run-001`), its
  reproducible builder `scripts/dst_build_corpus.py` (`--check` verifies), and
  `tests/dst_corpus/known_failures.json` for stress modes.
- Added `.github/workflows/dst-pr.yml` (corpus on every pull request, Ubuntu, artifacts
  retained 14 days) and `.github/workflows/dst-scheduled.yml` (nightly Ubuntu and
  Windows: rotating legacy/profile seed windows plus `stress_default`, artifacts
  retained 30 days, manual dispatch with explicit seed starts).

Implementation details:

- File inventory. New: `lib_dst/cli.py`, `lib_dst/__main__.py`,
  `lib_dst/artifacts.py`, `lib_dst/provenance.py`, `lib_dst/process.py`,
  `lib_dst/evaluation.py`, `lib_dst/diff.py`, `om-dst`, `om-dst.ps1`,
  `scripts/dst_build_corpus.py`, `tests/dst_corpus/` (`manifest.json`,
  `known_failures.json`, `traces/`), `tests/test_dst_phase5.py`, and the two workflows.
  Modified: `lib_dst/stress.py` (outcome/exit classification, identity, worker launch),
  `lib_dst/invariants.py` and `lib_dst/failures.py` (optional `evidence`),
  `lib_dst/worker.py` (hidden Phase 5 switches), `lib_dst/runner.py` (worker `cwd` and
  `PYTHONPATH`), `lib_dst/corpus.py` (shared atomic writer), `README.md`, `.gitignore`.
- Run identity. `run_id = <mode>-<first 16 hex of sha256(canonical command)>`. The
  command dict contains only result-relevant inputs: mode parameters, worker limits,
  and the shrink flag. `--out`, `--overwrite`, and `--resume` are excluded. Corpus runs
  identify the corpus by its `[entry id, trace sha]` pairs and stress runs embed the
  known-failures *content*, so run IDs do not depend on filesystem paths.
- Result statuses. Every summary row has `key`, `trace_sha256`, `classification`,
  `status`, and, where relevant, `expect` and `failure_artifact`. Statuses are `pass`,
  `fail` (failed verdict without a matching expectation), `xfail` (matches its
  expectation on `verdict`/`origin`/`kind`/`code`), `xpass` (expected failure now
  passes), `inconclusive`, and `boundary` (discovery `safety_ceiling` or
  `generation_rejected`). Exit precedence: any `fail`/`xpass` → 1, else any
  `inconclusive` → 3, else 0. For `shrink`, `pass` means minimized and reproduced in a
  clean worker.
- Summary identity. `summary.json` contains tool, artifact schema version, mode,
  command, ordered rows, status counts, exit code and mode extras (stress report path
  and identity, and for discovery `last_observed_pass`, `first_observed_failure`,
  `monotonic`). `identity_sha256` is the SHA-256 of that canonical JSON. Elapsed time,
  per-unit timings and units/second go only in `measurements.json`. The OM Core
  revision, dependency versions, coordinator environment, and Python executable go
  only in `manifest.json` provenance.
- Checkpoints and resume. A checkpoint is written only after a unit's failure
  artifacts (and any shrink) are complete: per corpus entry (`corpus:<id>`), per seed
  batch (`seeds-<start>-<count>`), per stress point (`stress:<axis>:<value>`). On resume,
  checkpointed units are reported from disk and the rest re-executed. Limit discovery
  re-runs its deterministic search with an executor that returns checkpointed points,
  so the bracket is identical. A checkpoint that fails to parse is treated as absent
  and recomputed.
- Worker protocol (internal, hidden from `--help`). `--evaluate-stdin` takes one trace
  and prints one JSON object: `trace_sha256`, `classification`,
  `deterministic_evidence`, full `outcome`, the failing `scheduled` report, `diffs`,
  `worker_environment`. `--seed-outcomes --start-seed S --count N [--profile P]`
  prints one line per seed. Passing lines are compact; non-passing lines add outcome,
  diffs, worker environment and the canonical `trace_json`. `--shrink-stdin` prints
  `status` (`minimized` or `not-failing`), signature, candidate count, and
  `minimized_json`. Existing switches are unchanged.
- Seed-batch localization. A batch's timeout is `--timeout × batch size`. When a batch
  ends abnormally, completed lines are kept and each unreported seed runs alone with the
  per-trace timeout. A seed that still has no result is classified from its exit evidence.
  The coordinator regenerates its trace under the pinned environment, so the published
  `trace.json` hashes to the reported `trace_sha256`.
- Evidence flow. `evaluate_trace` calls `capture_failure(trace, evidence=...)`.
  On an oracle mismatch it diffs `oracle.expected` against `engine.actual`. On an
  invariant mismatch it diffs the incremental observation against the failing check's
  observation (cache first/second recalculation, snapshot round trip, fresh-engine
  convergence). Diffs list `$.path` leaves in sorted order, compare types exactly (so
  `True != 1`), and are truncated at 500 entries with the full count recorded.
  `environment.json` holds the recorded and observed environments, field mismatches,
  revision, and dependency versions.
- Stress points still run through the Phase 4.5 `--stress-point-stdin` protocol, which
  supplies tracemalloc and persistence-size measurements. Only failing points with
  engine-attributed kinds (`correctness_mismatch`, `controlled_engine_error`) are
  re-evaluated in an `--evaluate-stdin` worker for outcome and diff evidence. `xfail`
  rows produce no failure artifacts and are not shrunk.
- Shrinking requires a normalized failure signature. Crashes, timeouts, and
  scheduler-only mismatches are published without `minimized.json`. `shrink.json`
  records the signature, candidate count, minimized hash, and
  `reproduced_in_clean_worker`; an abnormal shrink worker is recorded with its exit
  evidence instead.
- Replay compatibility compares `python`, `implementation`, `platform`,
  `python_hash_seed`, `timezone`, `engine_mode`, and `recalculation`. The coordinator
  predicts the worker descriptor (same interpreter, pinned seed and timezone), so
  `--strict-env` rejects before any worker or run directory is created.
- Corpus and known-failures schemas (version 1). `manifest.json` entries need a string
  `id`, string `trace` path, `trace_sha256`, and an object `expect`; `source` and
  `description` are documentation. `known_failures.json` maps
  `stress_axes.<axis>` to an expectation object; `reason` is documentation only.
- Tests in `tests/test_dst_phase5.py` (33):
  - exit-status classification, including a real abnormal child exit;
  - timeout and memory-limit evidence;
  - revision excluded from identity;
  - environment pinning and restoration;
  - diff stability;
  - observation evidence capture;
  - foreign-cwd runner;
  - run-directory collision, resume, overwrite, and foreign-directory protection;
  - byte-exact failure traces and atomic writes;
  - repeatable seed runs and interrupted-run resume;
  - seed localization on timeout;
  - replay failure artifacts and clean-worker shrink reproduction;
  - strict-environment rejection;
  - malformed input, arguments, corpus, and known failures;
  - coordinator exceptions exiting 3;
  - corpus pass/xfail/fail/xpass and hash tampering;
  - checked-in corpus consistency and builder `--check`;
  - known stress failures and the discovery safety ceiling;
  - shell-independent identity;
  - subprocess CLI from a foreign cwd with spaced paths;
  - the sh wrapper's exit code.

Decisions:

- Entry point is `python -m lib_dst` plus wrappers rather than a console script, so
  the application does not become an installable package.
- `run` covers corpus, seeds (legacy and profile), `stress_default`, single stress
  points, and limit discovery. Evolution remains a library API; it is not in the CLI.
- The CLI pins `PYTHONHASHSEED=1`/`TZ=UTC` while generating traces (and restores the
  caller's values afterwards), so generated traces record the environment their workers
  actually use. Library generation is unchanged. Cross-OS or cross-Python traces still
  differ because platform and Python version are recorded; replay warns on mismatch and
  `--strict-env` rejects it.
- `om_core_revision` is provenance, not identity: it was removed from
  `StressPointResult.identity_dict()` (still present in `to_dict()`), so report identity
  does not change on every commit. This changes stress identity hashes relative to the
  Phase 4.5 record.
- A coordinator-enforced timeout is classified `failed / policy / resource_exhaustion`
  with code `wall-timeout` and host-dependent detail, not `unknown_abnormal_exit`.
  Memory limits use `RLIMIT_AS` on POSIX; on Windows they are recorded as `unenforced`
  rather than implemented with Job Objects in this phase.
- Exit statuses are classified from positive evidence only. Windows NTSTATUS codes are
  normalized from their unsigned form; POSIX `SIGSEGV`/`SIGBUS`/`SIGILL`/`SIGFPE`/
  `SIGABRT` are crashes, `SIGXCPU`/`SIGXFSZ` resource exhaustion, and `SIGKILL` stays
  `unknown_abnormal_exit`.
- The two known OM Core defects are not repaired. They are expected failures in the
  corpus and in `known_failures.json`; a repair surfaces as `xpass` (exit 1) and forces
  the expectation to be updated. Declared persistence-fault traces are likewise
  expected failures with their exact normalized code.
- In limit discovery, `safety_ceiling` and `generation_rejected` points are reported as
  `boundary` and do not make the run inconclusive.
- Remote engine, solver, GUI, and multithreaded modes were not added; none has a
  deterministic boundary yet.
- Run artifacts default to `.om/dst-artifacts/` and are git-ignored with
  `/dst-artifacts/` and `/dst-summary.json`. CI publishes them as workflow artifacts;
  generated reports are never committed.
- The PR workflow runs only the fixed corpus (about 27 s locally), not the pytest suite.
  Seed batches and `stress_default` run on the nightly schedule. Nightly steps use
  `continue-on-error` so every campaign runs and uploads, and a final step fails the job
  if any did. Seed windows rotate by UTC day number and can be pinned through
  `workflow_dispatch` inputs.
- Corpus curation favours breadth over volume and is rebuilt only through
  `scripts/dst_build_corpus.py`. Historical Phase 4 traces are copied byte-for-byte
  (their recorded environment predates pinning). `.om/dst-corpus-run-001/` is left in
  place as the source of those traces; whether to remove it from version control is the
  maintainer's call.
- A coordinator exception exits 3, never 1, so a harness defect cannot pose as an
  engine failure. Malformed corpus or known-failures input exits 2.
- The POSIX memory limit lowers only the soft `RLIMIT_AS`, capped at the existing hard
  limit, so requesting more than the hard limit cannot stop a worker from starting.
- `classify_outcome` was extracted from `evaluate_stress_trace` with an unchanged
  mapping, so Phase 4.5 stress classification is behaviourally identical.

Discoveries:

- Windows `subprocess` reports NTSTATUS exit codes unsigned (`0xC0000017` arrives as
  `3221225495`). The Phase 4.5 classifier only listed signed values, so real Windows
  memory/crash exits could never be recognized; they were safely but uselessly reported
  as unknown.
- Trace hashes depended on the coordinator shell: `run_stress_default(4500)` produced
  identity `f266ab8e…` with `PYTHONHASHSEED`/`TZ` unset and `a0cf5ece…` with
  `PYTHONHASHSEED=1 TZ=UTC`, neither matching the recorded Phase 4.5 `6ced5209…`.
  Results (18 pass, 2 known defects) were identical in all three.
- The legacy `run_batch` worker checks only the oracle and returns an aggregate digest,
  and every runner helper uses `check=True`; neither is suitable for attributing CI
  failures, hence the separate per-seed protocol.
- Worker startup dominates small traces: one evaluation costs about 1 s regardless of
  profile, while a 50-seed legacy batch takes about 2 s. Batching seeds per worker is
  therefore the main throughput lever. Corpus entries stay one-per-worker for isolation.
- `InvariantCheck` stores only observation digests, so canonical diffs needed the
  optional `evidence` side channel rather than a report-schema change.
- Pinning the environment inside the CLI process must be scoped and restored. Leaving
  `PYTHONHASHSEED`/`TZ` modified would change the descriptors of traces generated later
  in the same interpreter (for example in pytest).
- The root-level `trace.json` and the historical evolution traces record Python 3.14.0,
  `python_hash_seed: random`, and `timezone: system`. They replay with a mismatch
  warning and are rejected under `--strict-env`. The corpus traces record `win32`, so
  the Ubuntu PR job replays them with a warning; outcomes do not depend on it.
- Without an explicit `shell:`, GitHub Actions runs steps with `bash -e` (no
  `pipefail`). The first draft of the PR workflow piped through `tee` and would have
  reported success for a failing corpus; it was caught in self-review before any run.
- Writing files through Python text mode on this Windows checkout (`autocrlf=true`)
  produced mixed LF/CRLF endings in edited modules; they were normalized to CRLF.

Measured throughput (this Windows machine, Python 3.12.1, one worker at a time;
`measurements.json`): legacy seeds 22.5/s, `small` 20.1/s, `medium` 14.3/s, `large`
5.7/s; the 25-entry corpus 27 s; CLI `stress_default` about 25 s. Nightly defaults
(5000 legacy, 2000 small, 500 medium, 200 large, `stress_default`) take about
8 minutes locally. A million legacy seeds would need about 12 runner-hours per
night at this rate and is not adopted. Hosted-runner throughput is not yet measured.

Validation performed (2026-11-17):

- `.venv/Scripts/python.exe -m pytest tests/test_dst_phase5.py -q`: 33 passed.
- `.venv/Scripts/python.exe -m pytest tests -q`: 171 passed.
- Self-review fixes before commit: the PR workflow sets `shell: bash` so `pipefail`
  stops `| tee` from masking the exit code; an unexpected coordinator exception exits
  3 (not 1); a malformed corpus manifest or known-failures file exits 2; the POSIX
  memory limit lowers only the soft `RLIMIT_AS`, capped at the existing hard limit.
- `.venv/Scripts/python.exe -m compileall -q lib_dst scripts`: passed.
- `git diff --check`: passed.
- `python -m lib_dst run --corpus tests/dst_corpus`: 20 pass, 5 xfail, exit 0.
- CLI `run --stress-default --known-failures tests/dst_corpus/known_failures.json` from
  two shells (`PYTHONHASHSEED`/`TZ` unset, and `777`/`America/New_York`) produced the
  identical summary identity `a91dc35f…` and stress report identity
  `981c2c28988f69f1b0816e99ca4dad395b45531bf756532d0a88369b5f82fbc9`
  (18 pass, 2 xfail). This is the new pinned baseline for Windows/Python 3.12.1.
- A persistence-fault trace replayed and shrank through the CLI; the minimized trace
  reproduced the same signature in a clean worker.
- Not yet validated: the GitHub Actions workflows have not run on hosted runners, and
  POSIX memory enforcement and signal classification have been verified only by tests
  that run on Windows.

Open items after Phase 5:

- Run both workflows on hosted runners. Confirm the 8192 MB `--memory-mb` setting does
  not trip on Linux scientific-library address-space reservations, then record
  hosted-runner throughput and adjust the nightly counts from `measurements.json`.
- Record a Linux `stress_default` baseline identity alongside the Windows one; trace
  hashes legitimately differ by platform.
- Windows memory enforcement (Job Objects) remains unimplemented and is reported as
  `unenforced`.
- Repairing the UDF and formatting command-boundary defects remains out of scope. When
  either is fixed, update `tests/dst_corpus/manifest.json` (via the builder) and
  `known_failures.json`.
- Corpus-wide runs execute one worker at a time. Parallel workers (`--jobs`) were not
  added; ordering-stable aggregation would be needed first.
- When committing, set the executable bit on the POSIX wrapper:
  `git add --chmod=+x om-dst`.

---

## 13. Minimal Real-Engine Harness Shape

The first prototype must use production objects. This outline is intentionally focused on
the lifecycle rather than duplicating OM Core command DTO details:

```python
def run_trace(trace: TraceTranscript) -> Observation:
    providers = providers_from_trace(trace)
    workspace_adapter = InMemoryJsonAdapter()
    snapshot_adapter = InMemorySnapshotAdapter()

    runtime = create_runtime(
        workspace=Workspace.create("DST"),
        bus=MessageBus(),
        providers=providers,
        workspace_adapter=workspace_adapter,
        snapshot_adapter=snapshot_adapter,
        enable_solver=False,
        enable_multithread_recompute=False,
    )

    symbols = SymbolTable()
    try:
        for operation in trace.operations:
            result = execute_operation(runtime.session, symbols, operation)
            symbols.commit(operation, result)
            verify_step_contract(runtime, operation, result)

        runtime.engine.recalculate_all()
        return canonical_observation(runtime, symbols)
    finally:
        runtime.close()
```

Some parameters and helpers above are implementation work introduced by this plan; they
do not yet exist. That is deliberate and makes the required OM Core integration seams
explicit. A mock engine may test the harness itself, but it cannot serve as evidence that
OM Core satisfies a differential invariant.

---

## 14. Definition of Done

DST is considered established for a supported execution mode when:

1. A trace executes against the real OM Core engine.
2. Repeated execution from clean supported environments yields the same canonical result.
3. Every PRNG decision belongs to a recorded seed domain or is materialized in the trace.
4. Time, IDs, persistence, and scheduling use injected providers in the tested path.
5. The oracle is independent and limited to explicitly supported semantics.
6. Cold convergence uses a newly deserialized workspace and newly constructed engine.
7. Faults correspond to documented delivery or recovery contracts.
8. Failures can be replayed and minimized without relying on the original process state.
9. Parallel workers share no runtime singletons and coordinate corpus writes safely.
10. Unsupported modes are reported explicitly rather than being presented as deterministic.
