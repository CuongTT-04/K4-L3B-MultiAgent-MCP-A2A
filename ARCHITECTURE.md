# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case chạy tuần tự trong một phiên MCP. CLI (`cli.py`) emit `case_received` / `case_finalized`; mọi bước giữa do coordinator (`coordinator.py`) điều phối.

```text
input case
   │ case_received (CLI)
   ▼
coordinator ──task_assigned──▶ entity_specialist ──get_order×candidates, get_customer_history──▶ MCP
   │◀──────────handoff──────────┘   (resolved / ambiguous / not_found)
   │ resolved?
   ├─ no ─▶ output bảo thủ: insufficient_evidence / needs_investigation (không gọi specialist khác)
   └─ yes
      ├──task_assigned──▶ order_shipment_specialist ──get_order(cache), items, shipment_summary,
      │◀─────handoff────────┘                           sellers*, product_context*──▶ MCP
      ├──task_assigned──▶ payment-agent ──get_payment_timeline, get_refund_timeline──▶ MCP
      │                   policy-agent  ──get_policy──▶ MCP
      │◀─policy_decided + handoff─┘
      ▼
   conflict resolver (conflict.py) ─▶ output builder (output_builder.py)
      │  LLM synthesis (gpt-4o-mini) chỉ chọn trong tập issue đã có evidence; lỗi ⇒ luật tất định
      ▼
   verifier (verifier.py) ──verification_completed──▶ outputs/<case_id>.json
   │ case_finalized (CLI)
```

`*` = gọi có điều kiện (xem §2). Trace chỉ chứa sự kiện quan sát được: `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer (`entity_specialist`) | `AgentTask.input_data` = case (claimed order, `candidate_order_ids`, `customer_unique_id_hint`) | Probe từng candidate, xếp hạng, reject candidate sai; lấy lịch sử khách hàng | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` → handoff `coordinator` |
| Coordinator (`coordinator`) | Case + kết quả các specialist | Giao việc, chặn specialist khi entity chưa resolve, gom kết quả, gọi builder/verifier | Không gọi tool; chỉ đọc cache `get_order` của store | `task_assigned`, output cuối |
| Order/product (`order_shipment_specialist`) | `AgentTask.input_data`: `case`, `resolved_order_ids` từ `entity_specialist`, `investigation_scope` | Lấy order row, items, sellers, product; dựng `affected_entities` (order/item/seller/shipment ids) | `get_order`, `get_order_items`, `get_sellers` (chỉ khi `seller_delay` hoặc item thiếu seller_id), `get_product_context` (chỉ khi `include_product_context`) | `SpecialistResult.data` → handoff `coordinator` |
| Shipment (`order_shipment_specialist`) | Order row + `get_shipment_summary`, `opened_at`, `source_precedence` (tùy chọn) từ policy specialist | Verdict (`on_time`/`seller_delay`/`logistics_delay`/`lost`/`returned`/`conflicting`/`insufficient_evidence`), `timeline_complete`, `late_seller_ids`, responsible parties, conflict order↔shipment | `get_shipment_summary` | `shipment_analysis`, `responsible_parties`, `cause_codes`, `data_conflicts`, claim verdict giao hàng; trace `handoff` `SHIPMENT_<VERDICT>` |
| Payment/refund (`payment-agent`) | `order_id` đã resolve, order row (ngày mua, `order_status`) do coordinator chuyển tiếp | Tách lifecycle thật khỏi kịch bản gây nhiễu, tính captured/refunded/refundable, phát hiện payment issue | `get_payment_timeline`, `get_refund_timeline`; `get_order_payments` chỉ khi timeline lỗi | `PaymentFindings` (issues, facts, evidence) → handoff `coordinator` |
| Policy (`policy-agent`) | `policy_version` của case + issue do coordinator chốt | Áp rule của policy cho issue: refund, action, case_status, responsible parties | `get_policy` | `PaymentDecision` (`payment_analysis`, `financial_resolution`, actions); trace `policy_decided` |
| Conflict resolver (`conflict.py`) | `data_conflicts` của entity và shipment | Chuẩn hoá, khử trùng, tối đa 5 conflict; phát hiện conflict chưa giải quyết | Không gọi tool | `data_conflicts`; unresolved ⇒ `needs_investigation`, confidence ≤ 0.60 |
| Verifier (`verifier`) | Output đã build + tập evidence ref đã tiêu thụ trong case | Kiểm invariant (§6) và JSON Schema | Không gọi tool | `verification_completed` `PASSED`/`FAILED` + mã lỗi |

Áp dụng least privilege: `order_shipment_specialist` từ chối tool ngoài `ALLOWED_TOOLS` (`PermissionError`); payment/policy chỉ gọi 3 tool của mình; coordinator, conflict resolver, verifier và LLM không có quyền gọi MCP.

## 3. Entity resolution và A2A protocol

**Xếp hạng candidate.** Pool = `claimed_order_id` + `candidate_order_ids` (khử trùng, giữ thứ tự). Mỗi candidate được probe bằng `get_order`: điểm nền 1.0; `+10` nếu `customer_unique_id` khớp `customer_unique_id_hint`; `+3` nếu là claimed order. Candidate bị reject khi khách hàng lệch hint hoặc `get_order` bị từ chối.

| Kết quả | Điều kiện | Confidence |
| --- | --- | ---: |
| `resolved` | Một candidate còn lại, hoặc candidate đầu có điểm cao hơn hẳn; các candidate thấp hơn vào `rejected_candidates` | 0.95 |
| `ambiguous` | Nhiều candidate đồng điểm | 0.50 |
| `not_found` | Không candidate nào qua probe | 0.10 |

Chỉ `resolved` mới mở đường cho order/shipment/payment; còn lại output là `insufficient_evidence` / `needs_investigation`, không refund.

**Message envelope.** Coordinator gửi `AgentTask` (`task_id = "<task_type>-<case_id>"`, `case_id`, `assigned_to`, `task_type`, `input_data`); specialist trả `SpecialistResult` (`status`, `evidence_refs`, `data`, `errors`, `warnings`). Mọi event mang `case_id`; `task_id` nằm trong `attributes` của `task_assigned` và `handoff` để nối cặp.

**Handoff, timeout, chống vòng lặp.** Luồng là DAG cố định: mỗi specialist được giao đúng một lần mỗi case, không specialist nào giao việc tiếp cho agent khác, nên không có vòng lặp. Timeout HTTP của gateway: 300 s đọc, 30 s kết nối. Specialist lỗi được coordinator thay bằng kết quả `insufficient_evidence` thay vì chờ lại.

## 4. Evidence và conflict lifecycle

1. **Validate.** `EvidenceGateway.call` kiểm mọi response theo `mcp-evidence-response-v1`; response lỗi nghiệp vụ (`is_error`) ném `MCPToolError`.
2. **Lưu ref.** `EvidenceStore` cache theo `(case_id, tool, args)` và giữ danh sách ref theo case; payment agent giữ `evidence_by_tool`. Ref không bao giờ được tạo, sửa hay dùng lại giữa các case: khoá cache luôn chứa `case_id`.
3. **`tool_result_consumed`.** Store emit khi entity/shipment dùng evidence; payment agent tự emit cho 3 tool của mình (actor `payment-agent` / `policy-agent`). Cache hit không emit lại.
4. **Chọn source theo policy.**
   - *Payment:* timeline trộn lifecycle thật với một kịch bản gây nhiễu. Lifecycle thật được neo vào ngày mua trong `get_order`; refund và `reconciliation_mismatch` thuộc capture đứng ngay trước chúng cùng số tiền. Event ngoài lifecycle bị loại khỏi mọi tổng tiền.
   - *Shipment:* timestamp đọc từ `get_order` và `get_shipment_summary` theo `source_precedence` (mặc định `get_order` trước); lệch nhau thành conflict order↔shipment.
   - *Claim khách hàng* là cáo buộc, không bao giờ ghi đè dữ kiện MCP; claim cũng không được đưa vào prompt LLM.
5. **Conflict chưa giải quyết** có `selected_source = null`; builder ép `case_status = needs_investigation` và confidence ≤ 0.60.
6. **Map evidence vào output.** `evidence_refs` của output = hợp các ref do specialist trả về cho case; mỗi `claim_assessment` chỉ trỏ ref của specialist đã chấm claim đó. Verifier bắt ref lạ bằng `UNKNOWN_EVIDENCE_REF`. Mỗi ref được trích đều đỡ một field của output: `get_order` → `entity_resolution`; `get_customer_history` → `customer_context`; items/sellers/shipment → `affected_entities`, `shipment_analysis`; product context → phạm vi `include_product_context` của case; payment/refund timeline → `payment_analysis`; policy → `financial_resolution`, `resolution_actions`. Call bị từ chối không có ref nên không bao giờ được trích.
7. **Không bao giờ tự sinh ref.** Ref chỉ được chép từ response MCP. Bộ giả lập offline `scripts/mock_mcp.py` (dùng để test khi gateway quá tải) sinh ref có tiền tố `ev_SIMULATED_`; `day09 validate` và `day09 package` từ chối mọi artifact chứa tiền tố này, và cũng từ chối artifact chứa API key (`sk-team-…`, `sk-or-…`).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi mạng | 2 (backoff 0.5 s, 1 s) trong `EvidenceStore` | Specialist trả `insufficient_evidence`, không bịa dữ liệu | `handoff` với `error_count`; không có `tool_result_consumed` |
| Tool bị server từ chối (`MCPToolError`) | 0 | Như trên; `get_refund_timeline` lỗi = order không có refund | `handoff` `COMPLETED`/`FAILED` |
| Entity not found/ambiguous | 0 | Bỏ qua order/shipment/payment; `insufficient_evidence`, `needs_investigation`, refund 0 | `handoff` của `entity_specialist` |
| Source conflict | 0 | Chọn source theo policy; không chọn được ⇒ `selected_source = null`, `needs_investigation` | `data_conflicts` trong output |
| Invalid specialist result | 0 | Coordinator thay bằng kết quả `insufficient_evidence` | `handoff` `FAILED` |
| LLM lỗi/timeout/ra giá trị ngoài tập cho phép | 0 | Luật tất định chọn issue ưu tiên cao nhất | — |

**Tool discovery.** `day09 run` gọi `list_tools` trước khi chạy case và dừng nếu gateway không công bố đủ `PIPELINE_TOOLS`; không tool nào được gọi theo tên đoán.

**Query budget / cache.** Khoảng 9–10 call mỗi case: entity 2× `get_order` + `get_customer_history`; shipment `get_order_items` + `get_shipment_summary` (+ `get_sellers` khi cần, + `get_product_context` theo scope; `get_order` lấy từ cache); payment 3 call. `get_order_payments` bị bỏ vì `get_payment_timeline` đã chứa payment rows. Cache theo case chặn gọi lặp giữa các specialist; không quét rộng theo khách hàng hay seller.

## 6. Verification invariants

Chạy trước khi ghi output (`verify_output`):

| Mã lỗi | Kiểm tra |
| --- | --- |
| `CASE_ID_MISMATCH` | `case_id` output = input |
| `SCHEMA_INVALID` | Output hợp lệ theo `l3b-output-v2.schema.json` |
| `UNKNOWN_EVIDENCE_REF` | Mọi ref trong output và claim đều đã được tiêu thụ trong case (evidence ownership) |
| `DUPLICATE_EVIDENCE_REF` / `DUPLICATE_RESOLUTION_ACTION` | Không trùng lặp |
| `ENTITY_SET_OVERLAP` | `resolved_order_ids` ∩ `rejected_candidates` = ∅ |
| `RESOLVED_ORDER_NOT_AFFECTED` | Order đã resolve nằm trong `affected_entities.order_ids` (entity scope) |
| `LATE_SELLER_NOT_AFFECTED` | Seller trễ nằm trong `affected_entities.seller_ids` |
| `REFUND_SUM_MISMATCH` | Tổng `refund_lines` = `recommended_refund_brl` (±0.01) |
| `REFUNDED_EXCEEDS_CAPTURED` | refunded ≤ captured |
| `NO_ACTION_WITH_REFUND` | `no_action` không đi kèm refund dương |
| `CONFIDENCE_OUT_OF_RANGE` | confidence ∈ [0, 1] |

Nhất quán responsibility/action: refund, action, status và responsible parties luôn được tính lại từ rule policy của **primary issue cuối cùng**, sau bước LLM, nên không thể lệch nhau. `CASE_ID_MISMATCH` và `SCHEMA_INVALID` là lỗi chặn (output không ghi được); các mã khác được ghi vào `verification_completed` (`FAILED`, `error_codes`) và output vẫn được giữ, để một case lỗi không làm mất 99 case còn lại.

## 7. Reproducibility

| Hạng mục | Giá trị |
| --- | --- |
| Python | ≥ 3.11 (CI 3.11; chạy thử 3.13) |
| Dependency | Khoảng phiên bản ghim trong `pyproject.toml` (`mcp>=2,<3`, `httpx2>=2,<3`, `jsonschema>=4.25,<5`, `python-dotenv>=1.1,<2`) |
| LLM | `openai/gpt-4o-mini` qua OpenRouter (đổi bằng `OPENROUTER_MODEL`), `temperature = 0`, `max_tokens = 500`, `response_format` JSON Schema strict; không dùng seed. Chỉ chọn primary/secondary issue, status, confidence (bị chặn trần), cause code trong tập cho phép; không tạo số tiền, ID hay evidence |
| Concurrency | Case chạy tuần tự trong một phiên MCP; trong một order, shipment gọi song song 3 tool đọc |
| Cấu hình | `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`, `OPENROUTER_API_KEY` (không ghi giá trị key vào tài liệu) |
| Lệnh | `pip install -e ".[dev]"` → `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip` |
| Kiểm thử | `ruff check .` và `pytest -q` |
