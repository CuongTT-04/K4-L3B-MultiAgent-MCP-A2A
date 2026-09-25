from __future__ import annotations

from student_agent.conflict import has_unresolved_conflict, merge_conflicts


def test_merge_conflicts_deduplicates_in_stable_order() -> None:
    shipment = {
        "field": "order_delivered_customer_date",
        "sources": ["get_order", "get_shipment_summary"],
        "selected_source": None,
        "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
    }
    payment = {
        "field": "payment.total",
        "sources": ["get_payment_timeline", "get_order_payments"],
        "selected_source": "get_payment_timeline",
        "resolution_code": "AUTHORITATIVE_TIMELINE",
    }

    assert merge_conflicts([shipment], [payment, shipment]) == [shipment, payment]


def test_has_unresolved_conflict_only_for_null_selected_source() -> None:
    resolved = {
        "field": "payment.total",
        "sources": ["timeline", "rows"],
        "selected_source": "timeline",
        "resolution_code": "AUTHORITATIVE_TIMELINE",
    }
    unresolved = {
        "field": "shipment.status",
        "sources": ["order", "shipment"],
        "selected_source": None,
        "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
    }

    assert has_unresolved_conflict([resolved]) is False
    assert has_unresolved_conflict([resolved, unresolved]) is True


def test_merge_conflicts_drops_malformed_entries() -> None:
    assert merge_conflicts(
        [{"field": "missing-sources"}],
        [{"field": "x", "sources": ["only-one"]}],
        ["not-an-object"],
    ) == []
