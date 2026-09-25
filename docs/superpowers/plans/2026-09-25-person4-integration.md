# Person 4 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the independently testable coordinator, conflict resolution, output construction, and verification layer for L3B while real specialist agents are still under development.

**Architecture:** Normalized immutable dataclasses isolate the coordinator from teammate-specific classes. Pure conflict and output functions consume those contracts, the verifier enforces deterministic invariants plus the public schema, and an injected coordinator is tested with async fake specialists. `workflow.solve_case` remains the later composition point for real specialist adapters.

**Tech Stack:** Python 3.11+, dataclasses, asyncio callables, pytest 8, jsonschema via the existing `Contracts` class, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-25-person4-integration-design.md`

## Global Constraints

- Preserve the public `day09-l3b-output-v2` and `day09-trace-event-v1` contracts verbatim.
- Never create, alter, or reuse an MCP `evidence_ref` across cases.
- Do not import unfinished entity, shipment, or payment specialist modules.
- Do not emit `case_received` or `case_finalized`; the existing CLI owns both events.
- Do not invent business facts when a specialist reports insufficient evidence.
- Keep `workflow.solve_case(case, gateway, trace)` unchanged until real adapters exist.
- Every production behavior is introduced by a test that fails for the expected missing behavior first.

## Review Focus

- A specialist reports more than one issue with equal rank: selection must be deterministic and preserve the other issue as secondary.
- A material conflict is unresolved: status must become `needs_investigation` and confidence must not exceed `0.60`.
- Evidence is present in the output but absent from all specialist results: verification must fail with `UNKNOWN_EVIDENCE_REF`.
- Entity resolution fails: shipment and payment solvers must not run, and the conservative output must remain schema-valid.
- Monetary totals contain decimal rounding differences of one cent or less: refund-sum verification must use a one-cent absolute tolerance.

---

### Task 1: Normalized Integration Contracts

**Files:**
- Create: `src/student_agent/integration_models.py`
- Create: `tests/test_integration_models.py`

**Interfaces:**
- Consumes: Public L3B enum values from `contracts/schemas/l3b-output-v2.schema.json`.
- Produces: `IssueCandidate`, `ReportedConflict`, `EntityCustomerResult`, `OrderShipmentResult`, `PaymentPolicyResult`, and conservative `insufficient()` constructors used by every later task.

- [ ] **Step 1: Write failing validation tests**

```python
import pytest

from student_agent.integration_models import IssueCandidate, ReportedConflict


def test_issue_candidate_rejects_confidence_outside_unit_interval() -> None:
    with pytest.raises(ValueError, match="confidence"):
        IssueCandidate("refund_pending", "action_required", 1.1, "payment", 1)


def test_reported_conflict_requires_two_distinct_sources() -> None:
    with pytest.raises(ValueError, match="sources"):
        ReportedConflict("payment.total", ("payment", "payment"), None, "UNRESOLVED", True)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_integration_models.py -q`

Expected: collection fails because `student_agent.integration_models` does not exist.

- [ ] **Step 3: Implement immutable normalized contracts**

Implement frozen dataclasses with these exact public fields:

```python
@dataclass(frozen=True)
class IssueCandidate:
    issue: str
    case_status: str
    confidence: float
    source: str
    rank: int


@dataclass(frozen=True)
class ReportedConflict:
    field: str
    sources: tuple[str, ...]
    selected_source: str | None
    resolution_code: str
    material: bool


@dataclass(frozen=True)
class EntityCustomerResult:
    status: str
    resolved_order_ids: tuple[str, ...]
    rejected_candidates: tuple[str, ...]
    customer_unique_id: str | None
    related_order_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    conflicts: tuple[ReportedConflict, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderShipmentResult:
    item_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    shipment_ids: tuple[str, ...]
    verdict: str
    late_seller_ids: tuple[str, ...]
    timeline_complete: bool
    ranked_causes: tuple[dict[str, object], ...]
    responsible_parties: tuple[dict[str, object], ...]
    issue_candidates: tuple[IssueCandidate, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    conflicts: tuple[ReportedConflict, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaymentPolicyResult:
    payment_references: tuple[str, ...]
    verdict: str
    captured_total_brl: float | None
    refunded_total_brl: float | None
    refundable_total_brl: float | None
    refund_lines: tuple[dict[str, object], ...]
    resolution_actions: tuple[str, ...]
    claim_assessments: tuple[dict[str, object], ...]
    issue_candidates: tuple[IssueCandidate, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    conflicts: tuple[ReportedConflict, ...] = ()
    warnings: tuple[str, ...] = ()
```

Validate confidence in `[0, 1]`, rank in `[1, 100]`, and conflict sources as two or more distinct non-empty strings. Add `OrderShipmentResult.insufficient()` and `PaymentPolicyResult.insufficient()` returning schema-safe empty results with confidence `0.0`.

- [ ] **Step 4: Add literal tests for conservative constructors**

```python
def test_insufficient_results_are_conservative() -> None:
    shipment = OrderShipmentResult.insufficient()
    payment = PaymentPolicyResult.insufficient()
    assert (shipment.verdict, shipment.timeline_complete) == ("insufficient_evidence", False)
    assert payment.verdict == "insufficient_evidence"
    assert payment.captured_total_brl is None
    assert payment.refund_lines == ()
```

- [ ] **Step 5: Run Task 1 tests**

Run: `python -m pytest tests/test_integration_models.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/student_agent/integration_models.py tests/test_integration_models.py
git commit -m "feat: add normalized integration contracts"
```

### Task 2: Conflict Resolution

**Files:**
- Create: `src/student_agent/conflict.py`
- Create: `tests/test_conflict.py`

**Interfaces:**
- Consumes: The three normalized result classes and `ReportedConflict` from Task 1.
- Produces: `ConflictResolution(conflicts, has_unresolved_material)` and `resolve_conflicts(case, entity, shipment, payment)`.

- [ ] **Step 1: Write failing tests for reported and detected conflicts**

Use small literal dataclass fixtures. The tests must prove:

```python
def test_unresolved_material_conflict_sets_summary_flag() -> None:
    result = resolve_conflicts(CASE, ENTITY, SHIPMENT_WITH_UNRESOLVED_CONFLICT, PAYMENT)
    assert result.has_unresolved_material is True
    assert result.conflicts == ({
        "field": "shipment.verdict",
        "sources": ["shipment_summary", "order_status"],
        "selected_source": None,
        "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
    },)


def test_resolved_order_overrides_customer_claim_as_an_explicit_conflict() -> None:
    result = resolve_conflicts(CASE_CLAIMING_OTHER_ORDER, ENTITY, SHIPMENT, PAYMENT)
    assert result.conflicts[0]["selected_source"] == "entity_resolution"
    assert result.conflicts[0]["resolution_code"] == "MCP_ENTITY_CONFIRMED"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_conflict.py -q`

Expected: collection fails because `student_agent.conflict` does not exist.

- [ ] **Step 3: Implement the pure resolver**

```python
@dataclass(frozen=True)
class ConflictResolution:
    conflicts: tuple[dict[str, object], ...]
    has_unresolved_material: bool


def resolve_conflicts(
    case: dict[str, object],
    entity: EntityCustomerResult,
    shipment: OrderShipmentResult,
    payment: PaymentPolicyResult,
) -> ConflictResolution:
    ...
```

Merge reported conflicts in entity, shipment, payment order. Deduplicate on `(field, sources, selected_source, resolution_code)`. Detect a claimed-order mismatch only when entity resolution succeeded and the claimed ID is not among resolved orders. Preserve stable ordering and convert source tuples to schema-compatible lists.

- [ ] **Step 4: Add a deterministic deduplication test**

```python
def test_duplicate_reported_conflicts_are_emitted_once_in_stable_order() -> None:
    result = resolve_conflicts(CASE, ENTITY_WITH_DUPLICATE, SHIPMENT_WITH_DUPLICATE, PAYMENT)
    assert [item["field"] for item in result.conflicts] == ["shipment.verdict"]
```

- [ ] **Step 5: Run Task 2 tests**

Run: `python -m pytest tests/test_conflict.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/student_agent/conflict.py tests/test_conflict.py
git commit -m "feat: resolve cross-domain conflicts"
```

### Task 3: L3B Output Builder

**Files:**
- Create: `src/student_agent/output_builder.py`
- Create: `tests/test_output_builder.py`

**Interfaces:**
- Consumes: Normalized results and `ConflictResolution` from Tasks 1-2.
- Produces: `build_output(case, entity, shipment, payment, conflicts) -> dict[str, object]`.

- [ ] **Step 1: Write a failing schema-valid happy-path test**

Create literal entity, shipment, and payment fixtures with valid `ev_` references of at least 20 suffix characters. Assert key business fields and validate using the real contracts:

```python
def test_builds_schema_valid_l3b_output() -> None:
    output = build_output(CASE, ENTITY, SHIPMENT, PAYMENT, NO_CONFLICTS)
    CONTRACTS.validate_output(output, "test output")
    assert output["assessment"] == {
        "primary_issue": "refund_pending",
        "secondary_issues": ["late_delivery_logistics"],
        "case_status": "action_required",
        "confidence": 0.82,
    }
    assert output["evidence_refs"] == [ENTITY_EV, SHIPMENT_EV, PAYMENT_EV]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_output_builder.py::test_builds_schema_valid_l3b_output -q`

Expected: collection fails because `student_agent.output_builder` does not exist.

- [ ] **Step 3: Implement deterministic output assembly**

```python
def build_output(
    case: dict[str, object],
    entity: EntityCustomerResult,
    shipment: OrderShipmentResult,
    payment: PaymentPolicyResult,
    conflicts: ConflictResolution,
) -> dict[str, object]:
    ...
```

Select the issue candidate with the lowest rank, breaking ties by higher confidence and then source name. Preserve remaining distinct issues as secondary issues. Use the minimum specialist confidence. Cap it at `0.60` and force `needs_investigation` for unresolved material conflicts. Deduplicate IDs, actions, and evidence in first-seen order. Recommended refund equals the sum of refund-line amounts rounded to two decimals.

- [ ] **Step 4: Add failing conservative and conflict tests, then implement them**

```python
def test_unresolved_entity_produces_conservative_schema_valid_output() -> None:
    output = build_output(CASE, NOT_FOUND_ENTITY, OrderShipmentResult.insufficient(), PaymentPolicyResult.insufficient(), NO_CONFLICTS)
    CONTRACTS.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_unresolved_material_conflict_caps_confidence() -> None:
    output = build_output(CASE, ENTITY, SHIPMENT, PAYMENT, UNRESOLVED_CONFLICTS)
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] == 0.60
```

Run each new test before adding its production branch and confirm it fails on the missing behavior.

- [ ] **Step 5: Run Task 3 tests**

Run: `python -m pytest tests/test_output_builder.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/student_agent/output_builder.py tests/test_output_builder.py
git commit -m "feat: build deterministic l3b outputs"
```

### Task 4: Deterministic Output Verifier

**Files:**
- Create: `src/student_agent/verifier.py`
- Create: `tests/test_verifier.py`

**Interfaces:**
- Consumes: Case, built output, available specialist evidence, and existing `Contracts`.
- Produces: `VerificationResult(passed, error_codes)` and `verify_output(...)`.

- [ ] **Step 1: Write parameterized failing invariant tests**

Create one known-good literal output, mutate one field per test, and assert these exact error codes:

```python
@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda out: out.update(case_id="OTHER_CASE"), "CASE_ID_MISMATCH"),
        (add_unknown_evidence, "UNKNOWN_EVIDENCE_REF"),
        (overlap_resolved_and_rejected, "ENTITY_SET_OVERLAP"),
        (remove_resolved_from_affected, "RESOLVED_ORDER_NOT_AFFECTED"),
        (set_refund_sum_mismatch, "REFUND_SUM_MISMATCH"),
        (set_refunded_above_captured, "REFUNDED_EXCEEDS_CAPTURED"),
        (set_no_action_with_refund, "NO_ACTION_WITH_REFUND"),
        (set_unknown_late_seller, "LATE_SELLER_NOT_AFFECTED"),
    ],
)
def test_verifier_reports_invariant(mutate, code) -> None:
    output = deepcopy(GOOD_OUTPUT)
    mutate(output)
    result = verify_output(CASE, output, KNOWN_REFS, CONTRACTS)
    assert code in result.error_codes
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_verifier.py -q`

Expected: collection fails because `student_agent.verifier` does not exist.

- [ ] **Step 3: Implement all invariants and schema validation**

```python
@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    error_codes: tuple[str, ...]


def verify_output(
    case: dict[str, object],
    output: dict[str, object],
    available_evidence_refs: set[str],
    contracts: Contracts,
) -> VerificationResult:
    ...
```

Accumulate stable error codes without duplicates. Compare refund sums with `math.isclose(..., abs_tol=0.01, rel_tol=0.0)`. Catch `ContractError` and add `SCHEMA_INVALID` instead of leaking the schema message into the stable result.

- [ ] **Step 4: Add the one-cent tolerance and success tests**

```python
def test_verifier_accepts_one_cent_refund_rounding_tolerance() -> None:
    output = deepcopy(GOOD_OUTPUT)
    output["financial_resolution"]["recommended_refund_brl"] += 0.01
    assert verify_output(CASE, output, KNOWN_REFS, CONTRACTS).passed is True


def test_verifier_accepts_known_good_output() -> None:
    assert verify_output(CASE, GOOD_OUTPUT, KNOWN_REFS, CONTRACTS) == VerificationResult(True, ())
```

- [ ] **Step 5: Run Task 4 tests**

Run: `python -m pytest tests/test_verifier.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/student_agent/verifier.py tests/test_verifier.py
git commit -m "feat: verify l3b output invariants"
```

### Task 5: Injected Coordinator and Trace Lifecycle

**Files:**
- Create: `src/student_agent/coordinator.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: Async normalized specialist callables, Tasks 1-4 modules, `TraceWriter`-compatible `emit`, and `Contracts`.
- Produces: `CoordinatorDependencies` and `coordinate_case(case, trace, dependencies, contracts)`.

- [ ] **Step 1: Write a failing happy-path orchestration test**

Use async fake functions returning literal normalized results and a recording trace double. Assert the real coordinator output and event order, not properties of mock objects:

```python
def test_coordinate_case_runs_specialists_and_verifies_before_return() -> None:
    trace = RecordingTrace()
    output = asyncio.run(coordinate_case(CASE, trace, DEPENDENCIES, CONTRACTS))
    CONTRACTS.validate_output(output, "coordinator output")
    assert [event["event_type"] for event in trace.events] == [
        "task_assigned", "handoff",
        "task_assigned", "handoff",
        "task_assigned", "handoff",
        "policy_decided", "verification_completed",
    ]
    assert trace.events[-1]["decision_code"] == "PASSED"
```

Use `asyncio.run` in synchronous pytest tests so the independent layer adds no dependency.

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_coordinator.py::test_coordinate_case_runs_specialists_and_verifies_before_return -q`

Expected: collection fails because `student_agent.coordinator` does not exist.

- [ ] **Step 3: Implement dependency injection and orchestration**

```python
EntitySolver = Callable[[dict[str, object]], Awaitable[EntityCustomerResult]]
ShipmentSolver = Callable[[dict[str, object], EntityCustomerResult], Awaitable[OrderShipmentResult]]
PaymentSolver = Callable[[dict[str, object], EntityCustomerResult], Awaitable[PaymentPolicyResult]]


@dataclass(frozen=True)
class CoordinatorDependencies:
    solve_entity_customer: EntitySolver
    solve_order_shipment: ShipmentSolver
    solve_payment_policy: PaymentSolver


async def coordinate_case(
    case: dict[str, object],
    trace: TraceWriter,
    dependencies: CoordinatorDependencies,
    contracts: Contracts,
) -> dict[str, object]:
    ...
```

Run entity first. Run shipment then payment only for `entity.status == "resolved"` with at least one resolved order. Emit `task_assigned` before each call and `handoff` after each result. Emit `policy_decided` after payment, then resolve, build, verify, and emit `verification_completed`. Raise `ValueError` containing stable verifier codes after emitting a failed verification event.

- [ ] **Step 4: Add unresolved-entity short-circuit test**

```python
def test_unresolved_entity_skips_domain_specialists() -> None:
    output, calls, events = asyncio.run(run_not_found_scenario())
    assert calls == ["entity"]
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert events[-1]["event_type"] == "verification_completed"
```

- [ ] **Step 5: Add verification-failure ordering test**

```python
def test_failed_verification_is_traced_before_error() -> None:
    with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE_REF"):
        asyncio.run(run_unknown_evidence_scenario())
    assert TRACE.events[-1]["event_type"] == "verification_completed"
    assert TRACE.events[-1]["decision_code"] == "FAILED"
```

- [ ] **Step 6: Run Task 5 and the complete suite**

Run: `python -m pytest tests/test_coordinator.py -q`

Expected: all coordinator tests pass.

Run: `python -m pytest -q`

Expected: all existing and new tests pass.

Run: `ruff check .`

Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add src/student_agent/coordinator.py tests/test_coordinator.py
git commit -m "feat: coordinate injected l3b specialists"
```

### Task 6: Independent-Layer Integration Check

**Files:**
- Create: `tests/test_person4_integration.py`
- Modify: `docs/superpowers/specs/2026-09-25-person4-integration-design.md`

**Interfaces:**
- Consumes: All Task 1-5 public interfaces.
- Produces: One end-to-end fake-specialist proof and accurate documentation of the implemented independent layer.

- [ ] **Step 1: Write the failing integration test**

Construct a representative late-shipment plus refund-pending case, run the injected coordinator, validate it with real contracts, and assert evidence-to-output linkage:

```python
def test_fake_specialists_produce_verified_traceable_submission_output() -> None:
    output, events = asyncio.run(run_representative_case())
    CONTRACTS.validate_output(output, "representative output")
    handed_off = {
        ref
        for event in events
        for ref in event.get("evidence_refs", [])
        if event["event_type"] == "handoff"
    }
    assert set(output["evidence_refs"]) <= handed_off
    assert output["assessment"]["primary_issue"] == "refund_pending"
```

- [ ] **Step 2: Run the integration test and verify RED**

Run: `python -m pytest tests/test_person4_integration.py -q`

Expected: the test fails until the recording trace fixture and full representative dependency set are wired.

- [ ] **Step 3: Add complete literal fixtures and make the test pass**

Keep all fakes in the test file. Mirror complete normalized results, including warnings, conflicts, claim assessments, evidence, causes, parties, totals, and actions. Do not add fake-only methods to production classes.

- [ ] **Step 4: Update the spec implementation-status paragraph**

Document that the independent layer is implemented and tested, while real specialist adapters, `workflow.solve_case` composition, and 100-case execution remain blocked on teammate deliverables. Do not claim the full lab is complete.

- [ ] **Step 5: Run final verification**

Run: `python -m pytest -q`

Expected: all tests pass.

Run: `ruff check .`

Expected: `All checks passed!`

Run: `git status --short`

Expected: only Task 6 files are modified before the commit.

- [ ] **Step 6: Commit**

```bash
git add tests/test_person4_integration.py docs/superpowers/specs/2026-09-25-person4-integration-design.md
git commit -m "test: verify person 4 integration layer"
```

## Deferred Integration Gate

After People 1-3 deliver, create a separate plan for adapters and `workflow.solve_case` composition. That plan must inspect their actual signatures before changing imports, must run `day09 run` against all 100 inputs, and must validate/package only after evidence-bearing end-to-end execution succeeds.
