# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | TODO | TODO | TODO | TODO |
| Coordinator | TODO | TODO | TODO | TODO |
| Order/product (`order_shipment_specialist`) | `AgentTask.input_data`: `case`, `resolved_order_ids` từ `entity_specialist`, `investigation_scope` | Lấy order row, items, sellers, product; dựng `affected_entities` (order/item/seller/shipment ids) | `get_order`, `get_order_items`, `get_sellers` (chỉ khi `seller_delay` hoặc item thiếu seller_id), `get_product_context` (chỉ khi `include_product_context`) | `SpecialistResult.data` → handoff `coordinator` |
| Shipment (`order_shipment_specialist`) | Order row + `get_shipment_summary`, `opened_at`, `source_precedence` (tùy chọn) từ policy specialist | Verdict (`on_time`/`seller_delay`/`logistics_delay`/`lost`/`returned`/`conflicting`/`insufficient_evidence`), `timeline_complete`, `late_seller_ids`, responsible parties, conflict order↔shipment | `get_shipment_summary` | `shipment_analysis`, `responsible_parties`, `cause_codes`, `data_conflicts`, claim verdict giao hàng; trace `handoff` `SHIPMENT_<VERDICT>` |
| Payment/refund | TODO | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO | TODO |
| Conflict resolver | TODO | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO | TODO |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
