# 📋 BẢN BÀN GIAO INTERFACE & HẠ TẦNG DÙNG CHUNG (TỪ NGƯỜI 1)

> **Người thực hiện:** Người 1 (Nền tảng Evidence + Entity/Customer)  
> **Gửi đến:** Người 2 (Order/Shipment), Người 3 (Payment/Policy), Người 4 (Coordinator/Verifier)  
> **Trạng thái:** ✅ Đã hoàn thành code, pass 100% unit tests & ruff lint.

---

## 1. Danh sách file Người 1 đã hoàn thiện và bàn giao

1. [`src/student_agent/models.py`](file:///home/hinhpython/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/models.py): Bộ shared types chuẩn cho giao thức A2A (`AgentTask`, `SpecialistResult`, `EntityResolutionData`, `OrderShipmentData`, `PaymentPolicyData`).
2. [`src/student_agent/evidence_store.py`](file:///home/hinhpython/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/evidence_store.py): Bộ nhớ đệm Evidence tập trung, có **Cache chống spam tool** (bảo vệ điểm *Efficiency*), cơ chế **Safe Retry**, và tích hợp **Trace Event**.
3. [`src/student_agent/specialists/base.py`](file:///home/hinhpython/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/specialists/base.py): Abstract Base Class `BaseSpecialist` để Người 2 và Người 3 kế thừa đồng bộ.
4. [`src/student_agent/specialists/entity_specialist.py`](file:///home/hinhpython/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/specialists/entity_specialist.py): Chuyên gia Entity Resolution xếp hạng candidate orders, phân loại rejected candidates và truy xuất customer history context.
5. [`tests/test_person1_foundation.py`](file:///home/hinhpython/K4-L3B-MultiAgent-MCP-A2A/tests/test_person1_foundation.py): Bộ Unit Test tự động kiểm thử toàn bộ models, cache, retry và logic nghiệp vụ.

---

## 2. Giao thức trao đổi Agent-to-Agent (A2A Protocol)

Mọi trao đổi giữa **Coordinator (Người 4)** và **Specialists (Người 1, 2, 3)** bắt buộc sử dụng 2 envelope chuẩn sau:

### 🔹 Input: `AgentTask`
Coordinator gửi nhiệm vụ cho Specialist:
```python
from student_agent.models import AgentTask, TaskType

task = AgentTask(
    task_id="task_001",
    case_id=case["case_id"],
    assigned_to="order_shipment_specialist",  # hoặc payment_policy_specialist
    task_type=TaskType.INVESTIGATE_ORDER_SHIPMENT,
    input_data={
        "case": case,
        "resolved_order_ids": entity_result.data["entity_resolution"]["resolved_order_ids"],
        # Các dữ liệu trung gian khác nếu cần
    },
    timeout_seconds=30.0
)
```

### 🔹 Output: `SpecialistResult`
Mọi Specialist trả kết quả về cho Coordinator:
```python
from student_agent.models import SpecialistResult, TaskStatus

result = SpecialistResult(
    task_id=task.task_id,
    case_id=task.case_id,
    actor="order_shipment_specialist",
    status=TaskStatus.COMPLETED,
    evidence_refs=["ev_abc123...", "ev_def456..."],  # Danh sách evidence_ref đã dùng
    data={
        # Payload kết quả chuyên môn (xem chi tiết mục 4 và 5)
    },
    errors=[],
    warnings=[]
)
```

---

## 3. Cách sử dụng `EvidenceStore` (Bắt buộc cho Người 2 & 3)

⚠️ **LƯU Ý SỐNG CÒN:** Không gọi trực tiếp `gateway.call(...)` mà hãy gọi qua `store` để:
1. **Không bị gọi trùng lặp**: Nếu cùng 1 `order_id` mà cả Người 1 và Người 2 đều cần, `EvidenceStore` sẽ lấy ngay từ RAM, không gửi request lên server MCP (tránh bị trừ điểm *Efficiency*).
2. **Tự động Retry an toàn**: Tự động thử lại khi gặp gián đoạn mạng hoặc timeout.
3. **Tự động gom Evidence Refs**: Coordinator chỉ cần gọi `store.get_case_evidence_refs(case_id)` là có đủ danh sách bằng chứng hợp lệ.

### Ví dụ gọi Tool trong Specialist:
```python
# Gọi tool an toàn:
evidence = await store.call_tool_safe(
    "get_order_items",
    case_id=case_id,
    actor=self.actor_name,
    order_id=target_order_id,
)

evidence_ref = evidence.get("evidence_ref") # ví dụ: "ev_..."
data = evidence.get("data", {})             # Dữ liệu nghiệp vụ thật
```

---

## 4. Hướng dẫn cụ thể cho NGƯỜI 2 (Order / Product / Shipment)

* **Tools phụ trách**: `get_order`, `get_order_items`, `get_product_context`, `get_sellers`, `get_shipment_summary`.
* **Kế thừa Class**:
```python
from student_agent.specialists.base import BaseSpecialist
from student_agent.models import AgentTask, SpecialistResult, TaskStatus

class OrderShipmentSpecialist(BaseSpecialist):
    def __init__(self):
        super().__init__(actor_name="order_shipment_specialist")

    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult:
        case_id = task.case_id
        resolved_orders = task.input_data.get("resolved_order_ids", [])
        
        # 1. Gọi store.call_tool_safe(...) cho items, product, shipment...
        # 2. Phân tích: trễ do seller hay logistics?
        # 3. Trả về kết quả:
        return SpecialistResult(
            task_id=task.task_id,
            case_id=case_id,
            actor=self.actor_name,
            status=TaskStatus.COMPLETED,
            evidence_refs=[...],
            data={
                "affected_entities": {
                    "order_ids": resolved_orders,
                    "item_ids": [...],
                    "seller_ids": [...],
                    "payment_references": [],
                    "shipment_ids": [...]
                },
                "shipment_analysis": {
                    "verdict": "seller_delay", # ["on_time", "seller_delay", "logistics_delay", "lost", "returned", "conflicting", "insufficient_evidence"]
                    "late_seller_ids": [...],
                    "timeline_complete": True
                }
            }
        )
```

---

## 5. Hướng dẫn cụ thể cho NGƯỜI 3 (Payment / Refund / Policy)

* **Tools phụ trách**: `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`, `get_policy`.
* **Kế thừa Class**:
```python
from student_agent.specialists.base import BaseSpecialist
from student_agent.models import AgentTask, SpecialistResult, TaskStatus

class PaymentPolicySpecialist(BaseSpecialist):
    def __init__(self):
        super().__init__(actor_name="payment_policy_specialist")

    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult:
        case_id = task.case_id
        policy_version = task.input_data.get("policy_version", "EC_POLICY_V2")
        
        # 1. Gọi store.call_tool_safe(...) lấy payments, refund timeline, policy
        # 2. Đối soát số tiền BRL
        # 3. Trả về kết quả:
        return SpecialistResult(
            task_id=task.task_id,
            case_id=case_id,
            actor=self.actor_name,
            status=TaskStatus.COMPLETED,
            evidence_refs=[...],
            data={
                "payment_analysis": {
                    "verdict": "reconciled", # ["reconciled", "capture_mismatch", "duplicate_capture", "refund_pending", "refund_failed", "refunded", "insufficient_evidence"]
                    "captured_total_brl": 150.0,
                    "refunded_total_brl": 0.0,
                    "refundable_total_brl": 150.0
                },
                "data_conflicts": [],
                "financial_resolution": {
                    "currency": "BRL",
                    "recommended_refund_brl": 150.0,
                    "refund_lines": [
                        {"reason_code": "late_delivery", "amount_brl": 150.0, "entity_id": "seller_1"}
                    ]
                }
            }
        )
```

---

## 6. Hướng dẫn cụ thể cho NGƯỜI 4 (Coordinator / Verifier)

Người 4 chỉ cần lắp ghép luồng trong `src/student_agent/workflow.py`:

```python
from student_agent.evidence_store import EvidenceStore
from student_agent.specialists.entity_specialist import EntitySpecialist
from student_agent.models import AgentTask, TaskType

async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
    case_id = case["case_id"]
    store = EvidenceStore(gateway=gateway, trace=trace)
    
    # 1. Gọi Người 1 (Entity Resolution)
    entity_spec = EntitySpecialist()
    entity_task = AgentTask(
        task_id=f"task_entity_{case_id}",
        case_id=case_id,
        assigned_to="entity_specialist",
        task_type=TaskType.RESOLVE_ENTITY,
        input_data=case
    )
    entity_res = await entity_spec.execute(entity_task, store)
    
    resolved_order_ids = entity_res.data["entity_resolution"]["resolved_order_ids"]
    
    # 2. Gọi Người 2 & Người 3 với resolved_order_ids...
    # 3. Gom all evidence_refs:
    all_refs = store.get_case_evidence_refs(case_id)
    
    # 4. Build output JSON theo contracts/schemas/l3b-output-v2.schema.json
    ...
```
