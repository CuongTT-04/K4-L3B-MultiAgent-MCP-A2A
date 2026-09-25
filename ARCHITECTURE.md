# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi input được xử lý độc lập theo `case_id`. CLI đọc và validate case set, sau đó tạo một `EvidenceStore` riêng cho từng case để không tái sử dụng evidence chéo case. Coordinator điều phối tuần tự ba pha: resolve entity/customer, điều tra order/product/shipment, rồi đối soát payment/refund/policy. Các specialist chỉ trả fact có nguồn; `output_builder` hợp nhất fact, conflict và quyết định policy, còn GPT-4o-mini chỉ được xếp hạng các issue/cause đã được specialist cho phép. Verifier kiểm tra schema và các invariant trước khi output được ghi ra đĩa.

```text
inputs/<case_id>.json
        │
        ▼
CLI: case_received ───────────────────────────────────────────────┐
        │                                                         │
        ▼                                                         │
Coordinator ──task_assigned──► Entity/customer specialist         │
        │                         │ get_order                      │
        │                         └ get_customer_history           │
        │◄──────────── handoff + resolved/rejected candidates      │
        │                                                         │
        ├──task_assigned──► Order/product/shipment specialist      ├──► trace.jsonl
        │                     │ get_order / get_order_items        │
        │                     │ get_shipment_summary               │
        │                     └ get_sellers / get_product_context  │
        │◄──────────── handoff + shipment facts/conflicts          │
        │                                                         │
        ├──task_assigned──► Payment/refund + Policy specialists    │
        │                     │ get_payment_timeline               │
        │                     │ get_refund_timeline                │
        │                     └ get_policy                         │
        │◄──────────── handoff + policy_decided                    │
        │                                                         │
        ▼                                                         │
Conflict merge → deterministic facts → GPT-4o-mini ranking        │
        │                                                         │
        ▼                                                         │
Verifier ──verification_completed──► outputs/<case_id>.json        │
        │                                                         │
        └──────────────────── case_finalized ─────────────────────┘
```

Luồng không cho model tự tạo identifier, evidence, số tiền, action hoặc cause. Khi OpenRouter lỗi hoặc trả giá trị ngoài allow-list, hệ thống dùng synthesis deterministic từ specialist facts.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer (`entity_specialist`) | Case gốc: `claimed_order_id`, `candidate_order_ids`, `customer_unique_id_hint`, investigation scope | Dedupe candidate, loại placeholder, lấy order, chấm điểm candidate, resolve/reject entity và lấy lịch sử khách hàng | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context`, evidence refs và notes trong `SpecialistResult`; handoff về `coordinator` |
| Coordinator (`coordinator`) | Case gốc, gateway, trace writer và các dependency specialist/synthesizer | Tạo `AgentTask`, điều phối đúng thứ tự, chặn downstream khi entity chưa resolve, chọn issue từ findings, phát `policy_decided`, gọi builder và verifier | Không query domain mới; đọc `get_order` qua cache để truyền order row cho payment | `task_assigned`, `handoff`, `policy_decided`, `verification_completed`; trả output cuối cho CLI |
| Order/product (`order_shipment_specialist`) | `AgentTask.input_data`: `case`, `resolved_order_ids` từ `entity_specialist`, `investigation_scope` | Lấy order row, items, sellers, product; dựng `affected_entities` (order/item/seller/shipment ids) | `get_order`, `get_order_items`, `get_sellers` (chỉ khi `seller_delay` hoặc item thiếu seller_id), `get_product_context` (chỉ khi `include_product_context`) | `SpecialistResult.data` → handoff `coordinator` |
| Shipment (`order_shipment_specialist`) | Order row + `get_shipment_summary`, `opened_at`, `source_precedence` (tùy chọn) từ policy specialist | Verdict (`on_time`/`seller_delay`/`logistics_delay`/`lost`/`returned`/`conflicting`/`insufficient_evidence`), `timeline_complete`, `late_seller_ids`, responsible parties, conflict order↔shipment | `get_shipment_summary` | `shipment_analysis`, `responsible_parties`, `cause_codes`, `data_conflicts`, claim verdict giao hàng; handoff về `coordinator` |
| Payment/refund (`payment-agent`) | Resolved order, order row/total và customer claims | Chuẩn hóa timeline, neo event vào lifecycle của order, loại distractor, tính captured/refunded/refundable, phát hiện mismatch, duplicate/split payment và trạng thái refund | `get_payment_timeline`, `get_refund_timeline`; `get_order_payments` chỉ là fallback khi payment timeline không dùng được | `PaymentFindings`, `payment_analysis`, payment issue candidates và evidence refs; handoff về `coordinator` |
| Policy (`policy-agent`) | `policy_version`, payment facts và issue do coordinator chọn | Đọc rule, xác định status, refund lines, action và responsible parties; không tự suy diễn khi rule/evidence thiếu | `get_policy` | `PaymentDecision`; trace `policy_decided` chứa decision code, payment verdict và refund được đề xuất |
| Conflict resolver (`conflict.py` + `output_builder`) | Conflict từ entity/shipment/payment và source metadata | Chuẩn hóa, dedupe tối đa 5 conflict; nhận diện conflict chưa resolve; hạ case về `needs_investigation` và cap confidence khi chưa chọn được authoritative source | Không gọi MCP | `data_conflicts`, trạng thái conflict và confidence ceiling được chuyển sang output builder |
| Synthesizer (`OpenRouterSynthesizer`) | Customer claims, specialist facts, allow-list issue/cause và confidence ceiling | Xếp hạng issue/cause bằng structured JSON; không được tạo facts hoặc value ngoài allow-list | Chỉ gọi OpenRouter Chat Completions với model cấu hình | `SynthesisDecision`; lỗi mạng/format/allow-list dùng deterministic fallback |
| Verifier (`verifier`) | Case gốc, output đã build, tập evidence refs đã thu thập và contracts | Validate schema, case/evidence/entity/refund/confidence/seller/action invariants; fail-fast trước khi finalize | Không gọi MCP hoặc LLM | `VerificationResult`; trace `verification_completed` với `PASSED`/`FAILED` và error count |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Candidate pool được tạo theo thứ tự `claimed_order_id` trước, sau đó đến `candidate_order_ids`, đồng thời loại trùng. Marker dạng `candidate-<số>` được reject cục bộ vì là placeholder, tránh một MCP call không có khả năng tạo evidence. Các candidate còn lại được kiểm tra bằng `get_order` và chấm điểm như sau:

- base score `1.0` khi `get_order` trả evidence hợp lệ;
- cộng `10.0` nếu `customer_unique_id` khớp hint;
- trừ `5.0` và reject nếu customer ID mâu thuẫn với hint;
- cộng `3.0` nếu candidate chính là `claimed_order_id`.

Candidate đứng đầu được resolve nếu chỉ còn một ứng viên hợp lệ hoặc có score cao hơn hẳn ứng viên thứ hai. Trạng thái/confidence là `resolved/0.95`, `ambiguous/0.50` hoặc `not_found/0.10`. Khi có customer hint hoặc customer ID từ order, specialist gọi `get_customer_history` để điền `related_order_ids`; lịch sử này không làm thay đổi order đã resolve nếu thiếu evidence đối chiếu.

A2A dùng hai envelope nội bộ:

- `AgentTask`: `task_id`, `case_id`, `assigned_to`, `task_type`, `input_data`, `timeout_seconds` (mặc định 30 giây);
- `SpecialistResult`: `task_id`, `case_id`, `actor`, `status`, `evidence_refs`, `data`, `errors`, `warnings`.

`task_id` được tạo theo `<task_type>-<case_id>` và mọi trace/handoff giữ nguyên `case_id`, nên kết quả không thể ghép nhầm case. Coordinator chỉ tạo tối đa một task cho mỗi pha và không cho specialist tự giao việc tiếp, vì vậy không có vòng lặp A2A. Entity phải ở trạng thái `resolved` và có ít nhất một `resolved_order_id` trước khi chạy order/shipment/payment; nếu không, coordinator tạo các fragment `insufficient_evidence` tại chỗ. Hiện tại `timeout_seconds` là metadata của protocol; timeout I/O thực tế do MCP/OpenRouter client kiểm soát.

Trace chỉ ghi lifecycle, actor, decision code, tool/evidence refs và số lỗi/cảnh báo; không ghi prompt riêng, chain-of-thought hoặc nội dung bí mật.

## 4. Evidence và conflict lifecycle

Mọi MCP response đi qua `EvidenceGateway`, được kiểm tra theo `mcp-evidence-response-v1.schema.json` trước khi specialist sử dụng. `EvidenceStore` giữ ba cấu trúc in-memory theo từng case: cache theo `(case_id, tool_name, sorted arguments)`, lookup theo `evidence_ref`, và danh sách refs đã tiêu thụ. Một `EvidenceStore` mới được tạo cho mỗi case nên evidence không thể tái sử dụng chéo case.

Khi một response hợp lệ được tiêu thụ, hệ thống:

1. giữ nguyên `evidence_ref` do MCP cấp, không sửa hoặc tự tạo ref;
2. lưu envelope vào cache để call trùng tham số không tạo network roundtrip mới;
3. emit `tool_result_consumed` với actor, tool name, evidence ref và attempt;
4. chuyển refs liên quan vào specialist handoff, claim assessment và top-level `evidence_refs`;
5. verifier đối chiếu toàn bộ top-level/claim refs với tập refs đã thu thập trong đúng case.

Order/shipment so sánh timestamp giữa `get_order` và `get_shipment_summary`. `source_precedence` có thể ưu tiên một nguồn; nếu không cấu hình thì order được ưu tiên mặc định. Conflict có cấu trúc `field`, `sources`, `selected_source`, `resolution_code`. `merge_conflicts` loại record sai cấu trúc, dedupe và giới hạn 5 mục. Nếu `selected_source` là `null`, conflict chưa resolve: case chuyển thành `needs_investigation`, confidence không vượt `0.6`, và hệ thống không biến giá trị thiếu thành fact.

Payment timeline được neo theo purchase/approval date của resolved order để tách event thật khỏi distractor. Refund events được nhóm theo amount và lấy trạng thái cuối cùng. Policy chỉ tạo refund/action từ rule hoặc payment facts đã xác minh; tổng refund lines tiếp tục được verifier đối chiếu với `recommended_refund_brl`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/transient exception qua `EvidenceStore` | Tối đa 2 retry sau lần đầu, delay `0.5s` rồi `1.0s` | Sau 3 lần thất bại, raise `RuntimeError`; specialist có thể trả `insufficient_evidence`/warning theo domain | Chỉ response được tiêu thụ mới emit `tool_result_consumed`; handoff chứa `error_count`/`warning_count` |
| MCP từ chối deterministic (`MCPToolError`) | 0 retry | Reject candidate hoặc đánh dấu tool/domain thiếu; không lặp lại call chắc chắn thất bại | Handoff `COMPLETED`/`FAILED` tùy specialist, không tạo evidence ref giả |
| Entity `not_found`/`ambiguous` | Không quét rộng thêm | Bỏ qua specialist downstream; shipment/payment và output dùng `insufficient_evidence`, refund `0`, status `needs_investigation` | `handoff` entity rồi `verification_completed` |
| Source conflict chưa resolve | 0 retry; conflict không phải lỗi transport | Giữ conflict, status `needs_investigation`, confidence tối đa `0.6` | `verification_completed`; conflict nằm trong `data_conflicts` |
| Payment/refund/policy tool thiếu | Mỗi tool gọi một lần; payment timeline có một fallback `get_order_payments` | `missing_tools`, nullable totals và verdict `insufficient_evidence`; không đoán số tiền | `handoff`, `policy_decided` với evidence hiện có |
| OpenRouter timeout/response sai schema/value ngoài allow-list | Không retry ở workflow | Dùng deterministic synthesis từ specialist facts, giữ confidence ceiling | Không ghi prompt; verifier vẫn chạy như bình thường |
| Invalid specialist/output result | 0 retry tự động | Verifier trả error codes và coordinator raise trước khi CLI ghi/finalize output | `verification_completed/FAILED` |

Query budget và cache strategy:

- candidate, order ID và customer ID đều đến từ input hoặc evidence; không có search toàn bảng;
- placeholder candidate bị reject trước MCP;
- `get_order` trùng case/order được `EvidenceStore` trả từ cache;
- `get_customer_history` chỉ gọi khi có customer ID;
- `get_sellers` chỉ gọi khi cần chứng minh seller responsibility hoặc item thiếu seller ID;
- `get_product_context` chỉ gọi khi `include_product_context=true`;
- `get_refund_timeline` phục vụ refund reconciliation, còn `get_order_payments` chỉ dùng khi payment timeline không khả dụng;
- 100 case chạy tuần tự để tránh burst/rate-limit; trong một resolved order, `get_order`, `get_order_items` và `get_shipment_summary` chạy đồng thời vì độc lập.

## 6. Verification invariants

Trước `case_finalized`, các invariant sau được bảo vệ bởi gateway, specialist, builder và verifier:

- output phải pass `day09-l3b-output-v2`; trace event phải pass `day09-trace-event-v1`;
- `output.case_id` phải bằng input case và mọi task/handoff/evidence phải cùng `case_id`;
- mọi top-level và claim-level `evidence_ref` phải thuộc tập refs thực sự thu thập trong case; top-level refs không trùng;
- `resolved_order_ids` và `rejected_candidates` không giao nhau;
- mọi resolved order phải xuất hiện trong `affected_entities.order_ids`;
- claim assessment chỉ dùng claim ID từ input và refs của domain liên quan;
- shipment verdict chỉ được dựng từ timeline đã parse; `late_seller_ids` phải nằm trong affected seller IDs;
- source conflict phải nêu đủ source, resolution code và selected source hợp lệ; conflict chưa resolve làm giảm status/confidence;
- `recommended_refund_brl` phải bằng tổng `refund_lines` trong sai số `0.01 BRL`;
- `refunded_total_brl` không được lớn hơn `captured_total_brl` quá `0.01 BRL`;
- case `no_action` không được đề xuất refund dương;
- responsible party/action phải đến từ shipment facts hoặc policy rule, không từ model;
- resolution actions không được trùng;
- assessment confidence phải là số trong `[0, 1]`, không vượt confidence ceiling của entity/shipment/payment và bị cap khi có conflict hoặc lựa chọn lệch claim đã được xác nhận;
- GPT-4o-mini chỉ có thể chọn issue/cause trong allow-list do specialist tạo; mọi output ngoài allow-list bị loại và dùng fallback.

Verifier trả danh sách error code ổn định như `CASE_ID_MISMATCH`, `UNKNOWN_EVIDENCE_REF`, `ENTITY_SET_OVERLAP`, `REFUND_SUM_MISMATCH`, `NO_ACTION_WITH_REFUND`, `CONFIDENCE_OUT_OF_RANGE`, `LATE_SELLER_NOT_AFFECTED` và `SCHEMA_INVALID`. Chỉ kết quả `PASSED` mới được CLI ghi và finalize.

## 7. Reproducibility

- Runtime: Python `>=3.11`; package `day09-l3b-student-agent==0.1.0`.
- Dependency ranges: `httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`; dev dùng `pytest>=8.4,<9`, `ruff>=0.12,<1`.
- LLM: OpenRouter Chat Completions, mặc định `OPENROUTER_MODEL=openai/gpt-4o-mini`, `temperature=0`, strict JSON Schema và `max_tokens=500`.
- Cấu hình bắt buộc trong `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`, `OPENROUTER_API_KEY`; không commit hoặc ghi giá trị secret vào tài liệu/trace/ZIP.
- Concurrency: case chạy tuần tự; order/items/shipment trong cùng order dùng `asyncio.gather`; không có worker pool liên case.
- Randomness: không dùng random seed cho decision; LLM dùng temperature 0. `event_id` và timestamp của trace là không deterministic nhưng không ảnh hưởng semantic output.
- Network limits: MCP client timeout tổng `300s` (`connect/write 30s`); OpenRouter timeout tổng `60s` (`connect/write/pool 20s`).
- Output chỉ được ghi sau khi pass contract và verifier; packaging chỉ chứa `manifest.json`, `trace.jsonl`, `outputs/*.json`.

Lệnh tái lập từ repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\day09.exe --root . validate-inputs
.\.venv\Scripts\day09.exe --root . run
.\.venv\Scripts\day09.exe --root . validate
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\day09.exe --root . package --output dist\submission.zip
```

Khi MCP server không khả dụng, không dùng fallback output toàn bộ case để nộp bài. Giữ submission gần nhất đã validate và chỉ rerun/package sau khi MCP hoạt động trở lại.
