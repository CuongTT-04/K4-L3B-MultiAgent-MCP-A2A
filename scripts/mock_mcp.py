"""Offline L3B MCP simulator and local scorecard.

Mimics the 10 gateway tools with the data shapes observed on the real server (probe of cases
002-009): each order's payment timeline holds its real lifecycle, anchored on the purchase day,
plus a distractor scenario that matches the customer's claim topic; refund amounts come from
fixed policy rules; `get_refund_timeline` fails when there is no refund; `candidate-NNN` ids
fail `get_order`. Every case gets a seeded ground truth so the pipeline can be scored locally.

Outputs produced here are NOT submittable: their evidence refs do not exist in the real MCP
audit (provenance hard gate). Use it only to test the pipeline while the gateway is down.

    python scripts/mock_mcp.py            # rules only, fast
    python scripts/mock_mcp.py --llm      # also call OpenRouter synthesis (needs .env key)

The script checks the README artifact format itself; `day09 validate/package` deliberately
reject its `ev_SIMULATED_` refs so a simulated run can never be submitted.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
import random
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from student_agent.cases import load_case_set
from student_agent.contracts import Contracts
from student_agent.coordinator import CoordinatorDependencies, coordinate_case
from student_agent.mcp_gateway import MCPToolError
from student_agent.payment_agent import investigate_payment
from student_agent.specialists import EntitySpecialist, OrderShipmentSpecialist
from student_agent.submission import SIMULATED_REF_PREFIX, validate_artifacts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]

TOOL_ARGS = {
    "get_order": "order_id",
    "get_order_items": "order_id",
    "get_order_payments": "order_id",
    "get_shipment_summary": "order_id",
    "get_sellers": "order_id",
    "get_policy": "policy_version",
    "get_customer_history": "customer_unique_id",
    "get_product_context": "order_id",
    "get_payment_timeline": "order_id",
    "get_refund_timeline": "order_id",
}
TOOL_DOMAIN = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
    "get_customer_history": "customer",
    "get_product_context": "product",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
}
ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
)
# Observed on the real gateway (EC_POLICY_V2).
POLICY_RULES: dict[str, tuple[str, str, float, str]] = {
    "canceled_order_paid": ("action_required", "issue_refund", 79.0, "platform"),
    "duplicate_charge": ("action_required", "refund_duplicate_charge", 64.0, "payment_provider"),
    "late_delivery_logistics": ("action_required", "refund_freight", 16.0, "logistics_provider"),
    "late_delivery_seller": ("action_required", "refund_freight", 18.0, "seller"),
    "payment_mismatch": ("action_required", "reconcile_payment", 35.0, "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", 52.0, "payment_provider"),
    "refund_pending": ("needs_investigation", "monitor_refund", 0.0, "payment_provider"),
    "unavailable_order_paid": ("action_required", "issue_refund", 89.0, "seller"),
    "unsupported_claim": ("no_action", "document_no_action", 0.0, "customer"),
    "valid_split_payment": ("no_action", "document_no_action", 0.0, "customer"),
}
PAYMENT_VERDICT = {
    "duplicate_charge": "duplicate_capture",
    "payment_mismatch": "capture_mismatch",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}
CLAIM_MATCHES_TRUTH = 0.3  # observed 2 of 7 probed cases


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _hex(seed: str, length: int = 12) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


@dataclass
class World:
    """Ground truth plus every tool payload for one case."""

    case_id: str
    order_id: str
    customer_unique_id: str
    truth: str
    claim: str
    payloads: dict[str, Any] = field(default_factory=dict)
    refund_events: list[dict[str, Any]] = field(default_factory=list)


def _template(
    topic: str, order_id: str, at: datetime, rng: random.Random
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Payment rows, payment events and refund events of one scenario on day `at`."""

    def row(seq: int, kind: str, amount: float) -> dict[str, Any]:
        return {"order_id": order_id, "payment_sequential": str(seq), "payment_type": kind,
                "payment_installments": "1", "payment_value": f"{amount:.2f}"}

    def event(hour: int, kind: str, amount: float, status: str) -> dict[str, Any]:
        return {"order_id": order_id, "event_at": _iso(at.replace(hour=hour)),
                "event_type": kind, "amount_brl": f"{amount:.2f}", "status": status}

    if topic in ("duplicate_charge", "valid_split_payment"):
        amount = 64.0 if topic == "duplicate_charge" else 44.5
        return (
            [row(1, "credit_card", amount), row(2, "voucher", amount)],
            [event(10, "captured", amount, "confirmed"),
             event(11, "captured", amount, "confirmed")],
            [],
        )
    if topic == "payment_mismatch":
        return (
            [row(1, "credit_card", 35.0)],
            [event(10, "captured", 35.0, "confirmed"),
             event(12, "reconciliation_mismatch", 35.0, "open")],
            [],
        )
    if topic in ("refund_failed", "refund_pending"):
        amount, status = (52.0, "failed") if topic == "refund_failed" else (89.0, "pending")
        refund_at = (at + timedelta(days=11)).replace(hour=9)
        refund = {"order_id": order_id, "event_at": _iso(refund_at),
                  "event_type": "refund_requested", "amount_brl": f"{amount:.2f}",
                  "status": status}
        captured = [event(10, "captured", amount, "confirmed")]
        return [row(1, "credit_card", amount)], captured, [refund]
    fixed = {"canceled_order_paid": 79.0, "unavailable_order_paid": 89.0,
             "late_delivery_seller": 18.0, "late_delivery_logistics": 16.0}
    amount = fixed.get(topic, round(rng.uniform(20, 150), 2))
    return [row(1, "credit_card", amount)], [event(10, "captured", amount, "confirmed")], []


def build_world(case: dict[str, Any]) -> World:
    case_id = case["case_id"]
    rng = random.Random(case_id)
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    claim = request["claims"][0]["topic"]
    truth = claim if rng.random() < CLAIM_MATCHES_TRUTH else rng.choice(
        [issue for issue in ISSUES if issue != claim]
    )
    customer = case["customer_unique_id_hint"]
    world = World(case_id, order_id, customer, truth, claim)

    opened = datetime.fromisoformat(case["opened_at"])
    purchase = (opened - timedelta(days=rng.randint(20, 70))).replace(hour=9, minute=0, second=0)
    limit = purchase + timedelta(days=3)
    estimated = purchase + timedelta(days=10)
    status = "delivered"
    carrier, delivered = purchase + timedelta(days=2), purchase + timedelta(days=9)
    if truth == "late_delivery_seller":
        carrier, delivered = purchase + timedelta(days=7), purchase + timedelta(days=14)
    elif truth == "late_delivery_logistics":
        delivered = purchase + timedelta(days=14)
    elif truth in ("canceled_order_paid", "unavailable_order_paid"):
        status = "canceled" if truth == "canceled_order_paid" else "unavailable"
        carrier = delivered = None

    seller = f"seller-{_hex(case_id + 'seller')}"
    product = f"product-{_hex(case_id + 'product')}"
    timeline = {
        "order_purchase_timestamp": _iso(purchase),
        "order_approved_at": _iso(purchase + timedelta(hours=1)),
        "order_delivered_carrier_date": _iso(carrier),
        "order_delivered_customer_date": _iso(delivered),
        "order_estimated_delivery_date": _iso(estimated),
    }
    world.payloads["get_order"] = {
        "order_id": order_id, "customer_id": f"customer-row-{order_id[:12]}",
        "order_status": status, **timeline,
    }
    world.payloads["get_order_items"] = [{
        "order_id": order_id, "order_item_id": "1", "product_id": product, "seller_id": seller,
        "shipping_limit_date": _iso(limit), "price": "59.90", "freight_value": "16.00",
    }]
    world.payloads["get_sellers"] = [{"seller_id": seller, "seller_city": "sao paulo",
                                      "seller_state": "SP"}]
    world.payloads["get_product_context"] = [{"product_id": product,
                                              "product_category_name": "utilidades_domesticas",
                                              "product_category_name_english": "housewares"}]
    events = [{"event_type": "carrier_handoff", "event_at": _iso(carrier)}] if carrier else []
    if delivered:
        events.append({"event_type": "delivered", "event_at": _iso(delivered)})
    world.payloads["get_shipment_summary"] = {
        "order_id": order_id, **timeline, "carrier_id": f"carrier-{_hex(case_id, 6)}",
        "seller_handoff_limits": [{"seller_id": seller, "shipping_limit_date": _iso(limit)}],
        "events": events,
    }
    world.payloads["get_customer_history"] = {
        "customer_unique_id": customer,
        "orders": [
            {"order_id": order_id, "order_status": status,
             "order_purchase_timestamp": _iso(purchase)},
            {"order_id": _hex(case_id + "other", 32), "order_status": "delivered",
             "order_purchase_timestamp": _iso(purchase - timedelta(days=200))},
        ],
    }

    rows, pay_events, refunds = _template(truth, order_id, purchase, rng)
    shift = rng.choice((-1, 1)) * rng.randint(20, 90)
    d_rows, d_events, d_refunds = _template(claim, order_id, purchase + timedelta(days=shift), rng)
    ordered = [(rows, pay_events), (d_rows, d_events)]
    rng.shuffle(ordered)
    world.payloads["get_order_payments"] = [r for part, _ in ordered for r in part]
    world.payloads["get_payment_timeline"] = {
        "order_id": order_id,
        "payments": world.payloads["get_order_payments"],
        "events": [e for _, part in ordered for e in part],
    }
    world.refund_events = refunds + d_refunds
    if world.refund_events:
        world.payloads["get_refund_timeline"] = {"order_id": order_id,
                                                 "events": world.refund_events}
    world.payloads["get_policy"] = {
        "currency": "BRL",
        "policy_version": case["policy_version"],
        "rules": {
            issue: {
                "case_status": case_status,
                "recommended_action": action,
                "refund_brl": refund,
                "responsible_parties": [{
                    "party_id": seller if party == "seller" else None,
                    "party_type": party,
                }],
            }
            for issue, (case_status, action, refund, party) in POLICY_RULES.items()
        },
    }
    return world


class MockGateway:
    """Drop-in stand-in for `EvidenceGateway` backed by generated worlds."""

    def __init__(self, worlds: dict[str, World], contracts: Contracts) -> None:
        self.worlds = worlds
        self.contracts = contracts
        self.calls: collections.Counter[tuple[str, str]] = collections.Counter()

    async def list_tools(self) -> list[str]:
        return sorted(TOOL_ARGS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls[(case_id, tool_name)] += 1
        world = self.worlds.get(case_id)
        required = TOOL_ARGS.get(tool_name)
        if world is None or required is None or not arguments.get(required):
            raise MCPToolError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        value = arguments[required]
        ok = {
            "order_id": value == world.order_id,
            "customer_unique_id": value == world.customer_unique_id,
            "policy_version": value == "EC_POLICY_V2",
        }[required]
        data = world.payloads.get(tool_name)
        if not ok or data is None:
            raise MCPToolError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        encoded = json.dumps(data, sort_keys=True).encode()
        evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            # Marked so `day09 validate/package` refuses it (fabricated refs = hard gate 0).
            "evidence_ref": f"{SIMULATED_REF_PREFIX}{secrets.token_urlsafe(24)}",
            "result_hash": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            "domain": TOOL_DOMAIN[tool_name],
            "data": json.loads(encoded),
            "warnings": [],
        }
        self.contracts.validate_evidence(evidence, f"mock {tool_name}")
        return evidence


def _prepare_root(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    (target / "outputs").mkdir(parents=True)
    (target / "traces").mkdir()
    shutil.copy2(ROOT / "case-set.json", target / "case-set.json")
    shutil.copytree(ROOT / "inputs", target / "inputs")
    shutil.copytree(ROOT / "contracts", target / "contracts")


def _expected(world: World) -> dict[str, Any]:
    case_status, action, refund, _ = POLICY_RULES[world.truth]
    return {
        "primary_issue": world.truth,
        "case_status": case_status,
        "action": action,
        "refund": refund,
        "payment_verdict": PAYMENT_VERDICT.get(world.truth, "reconciled"),
    }


def scorecard(
    worlds: dict[str, World], outputs: dict[str, dict[str, Any]], gateway: MockGateway,
    trace_events: list[dict[str, Any]],
) -> None:
    rows = collections.Counter()
    calibration: list[float] = []
    misses: collections.Counter[tuple[str, str]] = collections.Counter()
    for case_id, output in outputs.items():
        world, expected = worlds[case_id], _expected(worlds[case_id])
        assessment = output["assessment"]
        correct = assessment["primary_issue"] == expected["primary_issue"]
        rows["primary_issue"] += correct
        rows["case_status"] += assessment["case_status"] == expected["case_status"]
        rows["refund_brl"] += abs(
            output["financial_resolution"]["recommended_refund_brl"] - expected["refund"]
        ) <= 0.01
        rows["action"] += expected["action"] in output["resolution_actions"]
        rows["payment_verdict"] += output["payment_analysis"]["verdict"] == expected[
            "payment_verdict"
        ]
        entity = output["entity_resolution"]
        rows["entity_resolved"] += entity["resolved_order_ids"] == [world.order_id]
        rows["decoy_rejected"] += any(c.startswith("candidate-") for c in entity[
            "rejected_candidates"
        ])
        calibration.append(1 - (float(correct) - assessment["confidence"]) ** 2)
        if not correct:
            misses[(world.truth, assessment["primary_issue"])] += 1

    total = len(outputs)
    print(f"\n=== Local scorecard ({total} simulated cases; truth = claim in "
          f"{sum(w.truth == w.claim for w in worlds.values())}) ===")
    for name, hits in rows.items():
        print(f"{name:<16} {hits:>3}/{total}  {hits / total:6.1%}")
    print(f"{'calibration':<16} mean {sum(calibration) / total:.3f}")
    per_case = collections.Counter()
    for (case_id, _), count in gateway.calls.items():
        per_case[case_id] += count
    counts = sorted(per_case.values())
    print(f"{'mcp calls/case':<16} mean {sum(counts) / total:.1f}  min {counts[0]}  "
          f"max {counts[-1]}")
    by_tool = collections.Counter()
    for (_, tool), count in gateway.calls.items():
        by_tool[tool] += count
    print("calls by tool   ", dict(by_tool.most_common()))
    failed = [e for e in trace_events if e["event_type"] == "verification_completed"
              and e.get("decision_code") == "FAILED"]
    print(f"{'verifier FAILED':<16} {len(failed)} case(s)",
          collections.Counter(e["attributes"].get("error_codes") for e in failed) or "")
    if misses:
        print("primary misses (truth -> got):")
        for (truth, got), count in misses.most_common():
            print(f"   {truth:<24} -> {got:<24} x{count}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=".sim", help="simulated repo root (default .sim)")
    parser.add_argument("--llm", action="store_true", help="use OpenRouter synthesis")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    args = parser.parse_args()

    target = (ROOT / args.out).resolve()
    _prepare_root(target)
    case_set = load_case_set(ROOT)
    case_ids = args.cases.split(",") if args.cases else list(case_set.case_ids)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    worlds = {cid: build_world(case_set.cases[cid]) for cid in case_set.case_ids}
    gateway = MockGateway(worlds, contracts)
    trace = TraceWriter(target / "traces" / "trace.jsonl", contracts)

    synthesizer = None
    if args.llm:
        from student_agent.llm_synthesis import OPENROUTER_MODEL, OpenRouterSynthesizer

        load_dotenv(ROOT / ".env")
        synthesizer = OpenRouterSynthesizer(
            os.environ["OPENROUTER_API_KEY"],
            model=os.getenv("OPENROUTER_MODEL", OPENROUTER_MODEL),
        )
    deps = CoordinatorDependencies(
        EntitySpecialist(), OrderShipmentSpecialist(), investigate_payment, synthesizer
    )

    outputs: dict[str, dict[str, Any]] = {}
    for case_id in case_ids:
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await coordinate_case(case, gateway, trace, deps)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        (target / "outputs" / f"{case_id}.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        outputs[case_id] = output

    trace_events = [
        json.loads(line)
        for line in (target / "traces" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(case_ids) == len(case_set.case_ids):
        validate_artifacts(target, case_set, contracts, allow_simulated=True)
        print(f"README format: OK ({len(outputs)} outputs, {len(trace_events)} trace events) "
              f"-> {target}  [SIMULATED - never package or submit]")
    scorecard(worlds, outputs, gateway, trace_events)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
