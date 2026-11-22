"""用量计量领域服务：管理API调用用量计量、计费周期结算与迟到用量调整。

跨模块数据流：
- 消费 api_registry 共享快照 → 核验计费API
- 消费 subscription 共享快照 → 审批通过后初始化用量
- 消费 rate_limiter 快照 → 超限写入用量异常
- 写入 data/usage/ 共享文件 → call_audit 消费审计
- 计费周期结束后触发结算 → 写入不可变结算凭证
- 迟到补到的调用 → 按期判定结算口径，产出可对账的迟到用量调整明细

迟到用量归属规则（复核口径）：
- 实际发生月账期尚未确认 → 「当期更正」：用量并入原账期，随该账期一起结算，
  调整明细在结算后回链凭证。
- 实际发生月账期已经确认（凭证冻结）→ 「跨期调整」：原账单与历史明细一律不改，
  差额经复核确认后计入当前归入账期，调整明细保留原结算凭证号用于追溯。
- 同笔迟到明细按幂等键去重，重放只返回原调整记录，两个月合计始终只记一笔。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class UsageBillingService(BaseModuleService):
    MODULE = "usage_billing"
    ENTITY = "用量记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "调用方", "API名称", "调用次数", "数据行数", "计费周期", "费用金额", "结算状态"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "API名称"]
    STATUS_ORDER = ["计量中", "已出账", "已确认", "已结算"]
    ACTION_RULES = {"确认出账": "已出账", "标记确认": "已确认", "完成结算": "已结算"}
    NEGATIVE_ACTIONS = []

    # 迟到调整复核口径
    KIND_IN_PERIOD = "当期更正"
    KIND_CROSS_PERIOD = "跨期调整"

    def record_usage(self, api_name: str, caller: str, call_count: int = 1,
                     data_rows: int = 0, amount: float = 0.0) -> dict[str, Any]:
        """记录一次API调用用量（实时计量，归入当前账期）。"""
        billing_cycle = self._current_billing_cycle()
        return self._merge_usage_row(
            api_name, caller, billing_cycle, call_count, data_rows, amount)

    def backfill_usage(self, *, api_name: str, caller: str, occurred_at: str,
                       call_count: int = 1, data_rows: int = 0, amount: float = 0.0,
                       current_cycle: str | None = None,
                       source_ref: str = "", remark: str = "") -> dict[str, Any]:
        """补录一笔迟到的调用明细，并按期判定它适用的结算口径。

        参数：
        - occurred_at：调用实际发生时间（ISO 字符串或 YYYY-MM-DD），决定实际发生月
        - current_cycle：补录时所处账期，默认取当前自然月（测试可显式指定）

        返回 dict 含 adjustment（迟到调整明细）、replayed（是否为重放）、
        adjustment_kind（口径）与 scope_explanation（口径说明，供复核人员判定）。
        """
        from app.services import data_manager as dm

        occurrence_cycle = self._cycle_of(occurred_at)
        applied_default_cycle = current_cycle or self._current_billing_cycle()
        idem_key = self._idem_key(
            caller, api_name, occurred_at, call_count, data_rows, amount)

        # 幂等：同笔迟到明细重放直接返回原记录，不产生第二笔费用。
        existing = dm.find_late_adjustment_by_idem(idem_key)
        if existing is not None:
            return {
                "adjustment": existing,
                "replayed": True,
                "created": False,
                "adjustment_kind": existing.get("adjustment_kind"),
                "scope_explanation": self._scope_explanation(existing),
            }

        origin_voucher = dm.read_settlement_voucher(occurrence_cycle)
        origin_settled = bool(origin_voucher.get("frozen"))

        if origin_settled:
            # 原账期已确认：原单和历史明细禁止改写，差额归入当前账期。
            adjustment_kind = self.KIND_CROSS_PERIOD
            applied_cycle = applied_default_cycle
            origin_voucher_no = origin_voucher.get("voucher_no")
        else:
            # 原账期未确认：并入原账期正常计量，属于当期账单范围内的更正。
            adjustment_kind = self.KIND_IN_PERIOD
            applied_cycle = occurrence_cycle
            origin_voucher_no = None
            self._merge_usage_row(
                api_name, caller, occurrence_cycle, call_count, data_rows, amount)

        adjustment, created = dm.register_late_adjustment(
            idem_key=idem_key,
            caller=caller,
            api_name=api_name,
            call_count=call_count,
            data_rows=data_rows,
            amount=amount,
            occurrence_cycle=occurrence_cycle,
            applied_cycle=applied_cycle,
            adjustment_kind=adjustment_kind,
            origin_voucher_no=origin_voucher_no,
            source_ref=source_ref,
            remark=remark,
        )
        return {
            "adjustment": adjustment,
            "replayed": False,
            "created": created,
            "adjustment_kind": adjustment_kind,
            "scope_explanation": self._scope_explanation(adjustment),
        }

    def review_late_adjustment(self, adjustment_id: int, *, confirm: bool,
                               reviewer: str = "复核员",
                               remark: str = "") -> dict[str, Any] | None:
        """复核迟到调整：确认后差额才计入结算汇总，驳回则不参与计费。"""
        from app.services import data_manager as dm
        return dm.review_late_adjustment(
            adjustment_id, confirm=confirm, reviewer=reviewer, remark=remark)

    def list_late_adjustments(self, **filters: Any) -> list[dict[str, Any]]:
        """查询迟到用量调整明细（可按发生月/归入账期/复核状态过滤）。"""
        from app.services import data_manager as dm
        return dm.list_late_adjustments(**filters)

    def get_settlement_voucher(self, billing_cycle: str) -> dict[str, Any]:
        """读取账期结算凭证（已确认后为不可变冻结凭证）。"""
        from app.services import data_manager as dm
        return dm.read_settlement_voucher(billing_cycle)

    def reconcile_cycles(self, *cycles: str) -> dict[str, Any]:
        """对多个账期产出对账单，并校验跨期合计不重复计费。"""
        from app.services import data_manager as dm

        cycle_reports = [dm.build_cycle_reconciliation(cycle) for cycle in cycles]
        # 同一笔调整只在其归入账期计一次差额；这里按幂等键去重后再汇总。
        # 只有跨期调整构成凭证外差额，当期更正已包含在原账期账单内。
        seen_idem: set[str] = set()
        combined_amount = 0.0
        combined_calls = 0
        for report in cycle_reports:
            for adj in report["adjustments"]:
                if adj.get("review_status") != "已确认":
                    continue
                key = adj.get("idem_key", "")
                if key in seen_idem:
                    continue
                seen_idem.add(key)
                if adj.get("adjustment_kind") != self.KIND_CROSS_PERIOD:
                    continue
                combined_amount += float(adj.get("delta_amount", 0) or 0)
                combined_calls += int(adj.get("delta_calls", 0) or 0)
        return {
            "cycles": list(cycles),
            "cycle_reports": cycle_reports,
            "combined_final_amount": sum(r["final_amount"] for r in cycle_reports),
            "combined_final_calls": sum(r["final_calls"] for r in cycle_reports),
            "unique_confirmed_adjustments": len(seen_idem),
            "combined_late_delta_amount": combined_amount,
            "combined_late_delta_calls": combined_calls,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        result = super().run_action(entry_id, action)

        if action == "完成结算" and entry:
            from app.services.data_manager import settle_billing
            billing_cycle = entry.get("计费周期", self._current_billing_cycle())
            settlement = settle_billing(billing_cycle, use_auto_settlement=True)

            # 联动增强：结算完成写入调用审计记录
            from app.services.data_manager import write_audit, DATA_ROOT
            audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
            write_audit(audit_id, {
                "audit_id": audit_id,
                "api_name": entry.get("API名称", ""),
                "caller": entry.get("调用方", ""),
                "event": "billing_settled",
                "billing_cycle": billing_cycle,
                "voucher_no": settlement.get("voucher_no"),
                "total_amount": settlement.get("total_amount", 0),
                "total_calls": settlement.get("total_calls", 0),
            })

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    # ---------- 内部辅助 ----------

    def _merge_usage_row(self, api_name: str, caller: str, billing_cycle: str,
                         call_count: int, data_rows: int, amount: float) -> dict[str, Any]:
        """把一笔用量合并进指定账期的用量记录（不存在则新建），并写共享文件。"""
        from app.services.data_manager import write_usage

        rows = store.rows(self.MODULE)
        for row in rows:
            if (row.get("API名称") == api_name and row.get("调用方") == caller
                    and row.get("计费周期") == billing_cycle):
                old_count = int(row.get("调用次数", "0") or 0)
                old_rows = int(row.get("数据行数", "0") or 0)
                old_amount = float(row.get("费用金额", 0) or 0)
                row["调用次数"] = str(old_count + call_count)
                row["数据行数"] = str(old_rows + data_rows)
                row["费用金额"] = old_amount + amount
                write_usage(int(row.get("id", 0)), {
                    "usage_id": row.get("id"),
                    "record_no": row.get("记录编号"),
                    "caller": caller,
                    "api_name": api_name,
                    "call_count": old_count + call_count,
                    "data_rows": old_rows + data_rows,
                    "amount": old_amount + amount,
                    "billing_cycle": billing_cycle,
                    "settlement_status": row.get("status"),
                })
                return row

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        new_record = {
            "id": new_id,
            "记录编号": f"USAG-{new_id:04d}",
            "调用方": caller,
            "API名称": api_name,
            "调用次数": str(call_count),
            "数据行数": str(data_rows),
            "计费周期": billing_cycle,
            "费用金额": amount,
            "结算状态": self.STATUS_ORDER[0],
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        rows.append(new_record)
        write_usage(new_id, {
            "usage_id": new_id,
            "record_no": f"USAG-{new_id:04d}",
            "caller": caller,
            "api_name": api_name,
            "call_count": call_count,
            "data_rows": data_rows,
            "amount": amount,
            "billing_cycle": billing_cycle,
            "settlement_status": self.STATUS_ORDER[0],
        })
        return new_record

    @staticmethod
    def _idem_key(caller: str, api_name: str, occurred_at: str,
                  call_count: int, data_rows: int, amount: float) -> str:
        # 归一化实际发生时间，避免同一请求因时间格式不同被当成两笔。
        normalized = occurred_at.strip().replace(" ", "T")
        raw = "|".join([caller, api_name, normalized,
                        str(call_count), str(data_rows), str(amount)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _cycle_of(occurred_at: str) -> str:
        """从 ISO 时间或 YYYY-MM-DD 解析出实际发生月（YYYY-MM）。"""
        text = occurred_at.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m")
        except ValueError:
            # 容错：直接截前 7 位（YYYY-MM）。
            return text[:7]

    def _scope_explanation(self, adjustment: dict[str, Any]) -> str:
        """给复核人员的口径说明：为什么符合或超出当期账单范围。"""
        kind = adjustment.get("adjustment_kind")
        occurrence = adjustment.get("occurrence_cycle")
        applied = adjustment.get("applied_cycle")
        if kind == self.KIND_IN_PERIOD:
            return (
                f"调用实际发生于 {occurrence}，该账期尚未确认结算，"
                f"更正并入 {applied} 当期账单，属于当期账单范围内补计，不产生跨期差额。"
            )
        origin = adjustment.get("origin_voucher_no") or "原结算凭证"
        return (
            f"调用实际发生于 {occurrence}，但该账期已确认结算（凭证 {origin} 已冻结），"
            f"超出原当期账单范围；原单与历史明细保持不变，差额经复核后计入 "
            f"{applied} 账期，凭调整明细可回溯原凭证。"
        )

    def _current_billing_cycle(self) -> str:
        from datetime import timedelta, timezone
        from datetime import datetime
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")

    def domain_snapshot(self) -> dict[str, Any]:
        from app.services import data_manager as dm

        rows = store.rows(self.MODULE)
        usage_list = []
        for row in rows:
            usage_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "caller": row.get("调用方"),
                "api_name": row.get("API名称"),
                "call_count": row.get("调用次数"),
                "data_rows": row.get("数据行数"),
                "billing_cycle": row.get("计费周期"),
                "amount": row.get("费用金额"),
                "status": row.get("status"),
            })

        adjustments = dm.list_late_adjustments()
        review_summary = {"待复核": 0, "已确认": 0, "已驳回": 0}
        confirmed_delta_amount = 0.0
        for adj in adjustments:
            status = adj.get("review_status", "待复核")
            review_summary[status] = review_summary.get(status, 0) + 1
            # 快照里的确认差额只统计跨期调整；当期更正已在用量行内。
            if status == "已确认" and adj.get("adjustment_kind") == self.KIND_CROSS_PERIOD:
                confirmed_delta_amount += float(adj.get("delta_amount", 0) or 0)

        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(usage_list),
            "usages": usage_list,
            "total_amount": sum(float(r.get("amount", 0) or 0) for r in usage_list),
            "settled_count": sum(1 for r in usage_list if r.get("status") == "已结算"),
            "late_adjustment_count": len(adjustments),
            "late_adjustment_review": review_summary,
            "confirmed_late_delta_amount": confirmed_delta_amount,
            "late_adjustments": adjustments,
        }
