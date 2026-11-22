"""用量结算专用路由：迟到用量回填、复核、凭证与跨期对账。

与通用资源路由 resource.py 区分：这里承载 usage_billing 领域特有的
迟到用量调整流程，路径固定在 /api/usage-billing/* 下，避免与
/api/{module} 动态资源路由冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.usage_billing import UsageBillingService

router = APIRouter(prefix="/api/usage-billing", tags=["用量结算"])


class BackfillPayload(BaseModel):
    api_name: str
    caller: str
    occurred_at: str = Field(..., description="调用实际发生时间，ISO 字符串或 YYYY-MM-DD")
    call_count: int = 1
    data_rows: int = 0
    amount: float = 0.0
    current_cycle: str | None = Field(default=None, description="补录时所处账期 YYYY-MM")
    source_ref: str = ""
    remark: str = ""


class ReviewPayload(BaseModel):
    confirm: bool
    reviewer: str = "复核员"
    remark: str = ""


def _service() -> UsageBillingService:
    return UsageBillingService()


@router.post("/backfill")
def backfill_usage(payload: BackfillPayload) -> dict[str, Any]:
    """补录迟到调用明细，返回口径判定与可对账的迟到调整明细。"""
    result = _service().backfill_usage(
        api_name=payload.api_name,
        caller=payload.caller,
        occurred_at=payload.occurred_at,
        call_count=payload.call_count,
        data_rows=payload.data_rows,
        amount=payload.amount,
        current_cycle=payload.current_cycle,
        source_ref=payload.source_ref,
        remark=payload.remark,
    )
    return {"ok": True, **result}


@router.get("/late-adjustments")
def list_late_adjustments(
    occurrence_cycle: str | None = None,
    applied_cycle: str | None = None,
    review_status: str | None = None,
) -> dict[str, Any]:
    """查询迟到用量调整明细（实际发生月 / 归入账期 / 复核状态）。"""
    items = _service().list_late_adjustments(
        occurrence_cycle=occurrence_cycle,
        applied_cycle=applied_cycle,
        review_status=review_status,
    )
    return {"items": items, "total": len(items)}


@router.post("/late-adjustments/{adjustment_id}/review")
def review_late_adjustment(adjustment_id: int, payload: ReviewPayload) -> dict[str, Any]:
    """复核迟到调整：确认差额入账或驳回。"""
    record = _service().review_late_adjustment(
        adjustment_id, confirm=payload.confirm,
        reviewer=payload.reviewer, remark=payload.remark)
    if record is None:
        raise HTTPException(status_code=404, detail=f"迟到调整明细 {adjustment_id} 不存在")
    return {"ok": True, "entry": record}


@router.get("/vouchers/{billing_cycle}")
def get_voucher(billing_cycle: str) -> dict[str, Any]:
    """读取账期结算凭证：原账单金额、迟到差额与最终口径。"""
    voucher = _service().get_settlement_voucher(billing_cycle)
    if not voucher:
        raise HTTPException(status_code=404, detail=f"账期 {billing_cycle} 尚未结算")
    return voucher


@router.get("/reconciliation")
def reconcile(cycles: str) -> dict[str, Any]:
    """跨期对账：cycles 传逗号分隔的账期，如 ?cycles=2026-10,2026-11。"""
    cycle_list = [c.strip() for c in cycles.split(",") if c.strip()]
    if not cycle_list:
        raise HTTPException(status_code=400, detail="cycles 至少包含一个账期")
    return _service().reconcile_cycles(*cycle_list)
