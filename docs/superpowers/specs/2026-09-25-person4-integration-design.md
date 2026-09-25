# Person 4 Integration Design

## Goal

Build the coordinator-owned portion of the L3B workflow without waiting for the entity,
shipment, and payment specialists to finish. The deliverable must be testable with fake
specialists and require only thin adapters when the real specialist implementations arrive.

## Scope

This design covers:

- conflict resolution across specialist results;
- construction of a complete `day09-l3b-output-v2` object;
- deterministic verification before an output is returned;
- coordinator sequencing and observable trace events;
- fake specialist fixtures and integration tests;
- final documentation of the integrated architecture.

This design does not implement entity resolution, order/shipment investigation,
payment/refund investigation, or direct MCP calls. Those remain owned by the other three
team members.

## Chosen Approach

Use contract-first dependency injection. The coordinator consumes three asynchronous
callables and normalized mapping results instead of importing unfinished specialist modules.
Tests supply deterministic fake callables. When the real specialists are ready, small adapters
translate their result classes into the normalized coordinator contracts.

This approach is preferred over waiting for concrete classes because it unlocks independent
work now. It is preferred over creating placeholder specialist classes because placeholders
would duplicate teammates' work and create a later refactor.

## Components

### Normalized contracts

`src/student_agent/coordinator.py` defines the coordinator-facing result contracts. Each result
contains domain data, immutable evidence references, reported conflicts, confidence, and
warnings. Coordinator code depends only on these contracts.

The three inputs are:

- entity/customer result: entity-resolution status, resolved and rejected orders, customer ID,
  related orders, evidence, confidence, warnings;
- order/shipment result: affected item/seller/shipment/product identifiers, shipment verdict,
  timeline completeness, root-cause candidates, evidence, confidence, warnings;
- payment/policy result: payment verdict and totals, refund lines, policy actions, claim
  assessments, evidence, confidence, warnings.

All evidence references are copied from MCP responses by specialists. The coordinator never
creates or modifies an evidence reference.

### Conflict resolver

`src/student_agent/conflict.py` is a pure function. It receives the case and the three normalized
results, then returns schema-shaped conflict objects.

Rules:

- customer claims are allegations and cannot override MCP-backed domain facts;
- policy may select an action but cannot rewrite shipment or payment facts;
- an unresolved contradiction has `selected_source` set to `null` and uses the resolution code
  `UNRESOLVED_SOURCE_CONFLICT`;
- unresolved material conflicts force `needs_investigation` and cap final confidence at `0.60`;
- every conflict names at least two distinct sources.

### Output builder

`src/student_agent/output_builder.py` is a pure function that creates every required L3B field.
It merges identifiers and evidence deterministically, removes duplicates while retaining stable
order, and never adds properties outside the public schema.

The builder uses conservative fallbacks:

- unresolved entity: primary issue `insufficient_evidence`, status `needs_investigation`;
- missing shipment facts: shipment verdict `insufficient_evidence`, timeline incomplete;
- missing payment facts: payment verdict `insufficient_evidence`, unknown totals represented by
  `null`;
- no verified refund amount: recommended refund is zero with no fabricated refund lines.

Final confidence is the minimum available specialist confidence, capped at `0.60` when a
material unresolved conflict exists.

### Verifier

`src/student_agent/verifier.py` runs deterministic invariants and the existing public JSON
Schema validator. It returns a result containing `passed` and stable error codes.

Invariants include:

- input and output case IDs match;
- resolved and rejected orders are disjoint;
- resolved orders appear in affected order IDs;
- output evidence is a subset of evidence supplied by specialists;
- evidence, actions, and entity ID lists contain no duplicates;
- late sellers are affected sellers;
- refund-line amounts sum to the recommended refund;
- refunded total does not exceed captured total when both are known;
- `no_action` cannot recommend a positive refund;
- confidence is within `[0, 1]`;
- the complete output validates against `l3b-output-v2.schema.json`.

Verification failure raises a case-scoped error. The workflow does not silently emit a known
invalid output.

### Coordinator

`src/student_agent/coordinator.py` owns orchestration. It receives a case, trace writer, and
`CoordinatorDependencies` containing three async specialist callables.

Data flow:

1. assign entity/customer investigation;
2. receive and trace the entity handoff;
3. if an order is resolved, assign shipment and payment investigations;
4. receive and trace both domain handoffs;
5. resolve cross-domain conflicts;
6. build the output;
7. verify the output;
8. emit `verification_completed` and return only a verified output.

The existing CLI remains responsible for `case_received` and `case_finalized`. The coordinator
must not duplicate those events. Specialist adapters remain responsible for reporting which
tool results they consumed; the coordinator traces handoffs with those evidence references.

`src/student_agent/workflow.py` will remain a thin composition root. It will not import real
specialist modules until all three teammate contracts are available. Until that integration
step, coordinator tests use fake dependencies and the starter `solve_case` is not represented
as end-to-end complete.

## Failure Handling

A specialist failure is converted by its future adapter into an insufficient-evidence result
with a warning; no facts or evidence are invented. An unresolved entity prevents downstream
order-specific specialists from being called. The output remains conservative and requests
investigation where the schema permits it.

Retry behavior and MCP caching are outside the coordinator. The entity/evidence owner supplies
that behavior so retries are not accidentally multiplied at multiple layers.

## Testing Strategy

Development follows test-first red/green cycles.

- conflict tests cover MCP-versus-claim precedence, unresolved source conflicts, and material
  conflict detection;
- output-builder tests use hand-written normalized results and validate literal output fields;
- verifier tests mutate one invariant at a time and assert stable error codes;
- coordinator tests use asynchronous fake specialists and a real in-memory trace double to
  verify ordering and downstream short-circuit behavior;
- an integration test validates the built output with the repository's real `Contracts` class;
- the complete existing test suite and Ruff must remain green.

Tests assert observable output and trace behavior, not the internal implementation of fake
callables.

## Integration Contract with Teammates

Each teammate must provide one adapter-compatible result and document the evidence references
actually consumed. Adapters may rename or reshape fields, but may not infer new facts. The
coordinator-facing normalized contracts remain stable even if teammate result classes change.

Final integration work waits for:

- real specialist import paths and callable signatures;
- exact mappings from MCP data to normalized fields;
- evidence-consumption events emitted by the specialist layer;
- complete end-to-end execution across all 100 cases.

## Completion Criteria

The independent portion is complete when conflict, builder, verifier, and injected coordinator
tests pass; outputs created from representative fake results pass the L3B schema; required
coordinator trace events are ordered correctly; existing tests and Ruff pass; and no production
module imports unfinished teammate code.
