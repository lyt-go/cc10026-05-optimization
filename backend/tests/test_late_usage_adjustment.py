"""迟到用量调整与账期结算对账测试。

场景（题面）：十月最后一晚发生的请求十一月才补到，操作员可能已经确认十月结算。
- 场景 A：同笔迟到明细在十月结算提交「前」补到 → 当期更正，并入十月账单
- 场景 B：同笔迟到明细在十月结算提交「后」补到 → 跨期调整，差额计入十一月
两种场景都要核对：两个月合计只记一笔、原凭证可追溯、更正明细可对账、
重放幂等不重复计费、原单与历史明细不被改写。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEMP_DATA = tempfile.mkdtemp(prefix="cc_late_usage_")
os.environ["DATA_ROOT"] = TEMP_DATA

OCT = "2026-10"
NOV = "2026-11"
OCCURRED_AT = "2026-10-31T23:55:00+08:00"
API = "月末查询API"
CALLER = "华东订阅方"
CALL_COUNT = 20
DATA_ROWS = 200
AMOUNT = 80.0


def _reset_state() -> None:
    from app.seed import SEED_ROWS
    from app.store import store as s

    s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    if os.path.exists(TEMP_DATA):
        shutil.rmtree(TEMP_DATA, ignore_errors=True)


def _settle_october(svc) -> dict:
    """模拟操作员确认并提交十月结算，返回冻结凭证。"""
    from app.services import data_manager as dm

    # 十月已有一条正常计量记录
    svc._merge_usage_row(API, CALLER, OCT, call_count=100, data_rows=1000, amount=400.0)
    return dm.settle_billing(OCT)


class TestLateUsageAdjustment:
    def setup_method(self):
        _reset_state()

    # ========================================================
    # 场景 A：结算提交「前」补到 → 当期更正
    # ========================================================

    def test_backfill_before_settlement_is_in_period_correction(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        # 十月账期先有正常用量，但还没结算
        svc._merge_usage_row(API, CALLER, OCT, call_count=100, data_rows=1000, amount=400.0)

        result = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)

        adj = result["adjustment"]
        assert result["created"] is True
        assert result["replayed"] is False
        assert result["adjustment_kind"] == svc.KIND_IN_PERIOD
        # 口径判定：归入实际发生月，属于当期账单范围
        assert adj["occurrence_cycle"] == OCT
        assert adj["applied_cycle"] == OCT
        assert adj["origin_voucher_no"] is None
        assert "当期账单范围内" in result["scope_explanation"]

        # 用量已并入十月账期的同一条记录（不新建十一月记录）
        oct_usage = dm.list_usage(OCT)
        matching = [u for u in oct_usage if u.get("api_name") == API and u.get("caller") == CALLER]
        assert len(matching) == 1
        assert int(matching[0]["call_count"]) == 120
        assert float(matching[0]["amount"]) == 480.0

        # 提交十月结算：凭证金额即更正后的口径，并回链调整明细
        voucher = dm.settle_billing(OCT)
        assert voucher["frozen"] is True
        assert voucher["original_amount"] == 480.0
        assert voucher["total_amount"] == 480.0

        # 复核确认后，调整明细回链到十月凭证；当期更正不产生凭证外差额（防双重计费）
        reviewed = svc.review_late_adjustment(adj["adjustment_id"], confirm=True)
        assert reviewed["review_status"] == "已确认"
        assert reviewed["settlement_voucher_no"] == voucher["voucher_no"]
        assert reviewed["applied"] is True

        refreshed = dm.read_settlement_voucher(OCT)
        assert refreshed["late_adjustment_count"] == 0
        assert refreshed["late_adjustment_amount"] == 0.0
        assert refreshed["total_amount"] == 480.0

        # 十一月没有任何用量或调整归入
        assert dm.list_usage(NOV) == []
        nov_adjustments = dm.list_late_adjustments(applied_cycle=NOV)
        assert nov_adjustments == []

        # 两月对账：迟到差额为 0（已含在十月账单），合计仍只此一笔
        report = svc.reconcile_cycles(OCT, NOV)
        assert report["combined_late_delta_amount"] == 0.0
        assert report["combined_final_amount"] == 480.0
        assert report["unique_confirmed_adjustments"] == 1

    def test_replay_before_settlement_does_not_double_charge(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        first = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)
        # 同笔明细重放（例如补数任务重试）
        replay = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)

        assert replay["replayed"] is True
        assert replay["created"] is False
        assert replay["adjustment"]["adjustment_id"] == first["adjustment"]["adjustment_id"]

        # 调整明细只有一条，十月用量只被加过一次
        assert len(dm.list_late_adjustments()) == 1
        oct_usage = [u for u in dm.list_usage(OCT)
                     if u.get("api_name") == API and u.get("caller") == CALLER]
        assert int(oct_usage[0]["call_count"]) == CALL_COUNT
        assert float(oct_usage[0]["amount"]) == AMOUNT

        dm.settle_billing(OCT)
        # 复核确认后才计入已确认差额；无论是否确认，调整明细始终只有一笔
        svc.review_late_adjustment(first["adjustment"]["adjustment_id"], confirm=True)
        report = svc.reconcile_cycles(OCT, NOV)
        # 两个月合计只记一笔
        assert report["unique_confirmed_adjustments"] == 1
        assert len(dm.list_late_adjustments()) == 1

    # ========================================================
    # 场景 B：结算提交「后」补到 → 跨期调整
    # ========================================================

    def test_backfill_after_settlement_is_cross_period_adjustment(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        oct_voucher = _settle_october(svc)
        original_amount = oct_voucher["original_amount"]
        assert original_amount == 400.0

        result = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)

        adj = result["adjustment"]
        assert result["adjustment_kind"] == svc.KIND_CROSS_PERIOD
        assert adj["occurrence_cycle"] == OCT
        assert adj["applied_cycle"] == NOV
        # 保留十月原结算凭证号，可关联到当时已确认明细
        assert adj["origin_voucher_no"] == oct_voucher["voucher_no"]
        assert adj["review_status"] == "待复核"
        assert "超出原当期账单范围" in result["scope_explanation"]

        # 十月用量文件与凭证原金额保持不变（新调整不得让原单和历史明细对不上）
        oct_usage = [u for u in dm.list_usage(OCT)
                     if u.get("api_name") == API and u.get("caller") == CALLER]
        assert int(oct_usage[0]["call_count"]) == 100
        frozen_again = dm.read_settlement_voucher(OCT)
        assert frozen_again["original_amount"] == 400.0
        assert frozen_again["late_adjustment_amount"] == 0.0

        # 待复核的差额不参与计费
        nov_report = dm.build_cycle_reconciliation(NOV)
        assert nov_report["late_delta_amount"] == 0.0

        # 复核确认 → 差额计入十一月凭证；十月凭证依旧不动
        svc.review_late_adjustment(adj["adjustment_id"], confirm=True, reviewer="复核-张")
        nov_voucher = dm.settle_billing(NOV)
        assert nov_voucher["original_amount"] == 0.0
        assert nov_voucher["late_adjustment_amount"] == AMOUNT
        assert nov_voucher["total_amount"] == AMOUNT
        assert nov_voucher["late_adjustment_count"] == 1

        oct_final = dm.read_settlement_voucher(OCT)
        assert oct_final["original_amount"] == 400.0
        assert oct_final["total_amount"] == 400.0

        # 调整明细闭合追溯链：实际发生月、归入账期、原凭证、入账凭证齐全
        stored = dm.list_late_adjustments()[0]
        assert stored["settlement_voucher_no"] == nov_voucher["voucher_no"]
        assert stored["origin_voucher_no"] == oct_voucher["voucher_no"]

    def test_replay_after_settlement_does_not_double_charge(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        oct_voucher = _settle_october(svc)

        first = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)
        svc.review_late_adjustment(first["adjustment"]["adjustment_id"], confirm=True)

        # 十一月反复重放同一笔迟到明细
        for _ in range(3):
            replay = svc.backfill_usage(
                api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
                call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
                current_cycle=NOV)
            assert replay["replayed"] is True

        assert len(dm.list_late_adjustments()) == 1
        nov_voucher = dm.settle_billing(NOV)
        # 十一月凭证只确认了一笔差额
        assert nov_voucher["late_adjustment_count"] == 1
        assert nov_voucher["total_amount"] == AMOUNT

        # 两个月合计：十月 400 + 十一月迟到差额 80 = 480，且全平台只此一笔调整
        report = svc.reconcile_cycles(OCT, NOV)
        assert report["combined_final_amount"] == 480.0
        assert report["combined_late_delta_amount"] == AMOUNT
        assert report["unique_confirmed_adjustments"] == 1

        # 十月凭证在跨期场景下完全不变
        oct_report = next(r for r in report["cycle_reports"] if r["billing_cycle"] == OCT)
        assert oct_report["original_amount"] == 400.0
        assert oct_report["final_amount"] == 400.0
        assert oct_report["voucher_no"] == oct_voucher["voucher_no"]

    def test_rejected_adjustment_never_billed(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        _settle_october(svc)
        result = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)
        svc.review_late_adjustment(result["adjustment"]["adjustment_id"], confirm=False)

        nov_voucher = dm.settle_billing(NOV)
        assert nov_voucher["late_adjustment_count"] == 0
        assert nov_voucher["total_amount"] == 0.0

        report = svc.reconcile_cycles(OCT, NOV)
        assert report["combined_late_delta_amount"] == 0.0

    def test_settlement_voucher_is_immutable_on_repeated_settle(self):
        """重复结算返回同一冻结凭证，金额不被重算。"""
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        first = _settle_october(svc)
        second = dm.settle_billing(OCT)
        assert first["voucher_no"] == second["voucher_no"]
        assert first["settled_at"] == second["settled_at"]
        assert second["frozen"] is True

    def test_review_is_idempotent(self):
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        _settle_october(svc)
        result = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV)
        adj_id = result["adjustment"]["adjustment_id"]
        svc.review_late_adjustment(adj_id, confirm=True, reviewer="复核-张")
        again = svc.review_late_adjustment(adj_id, confirm=False, reviewer="复核-李")
        # 首次确认结论不被后续重复提交翻转
        assert again["review_status"] == "已确认"
        assert again["reviewer"] == "复核-张"

    def test_trace_chain_along_adjustment(self):
        """沿调整明细可查实际发生月、当前归入账期及原结算凭证。"""
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        oct_voucher = _settle_october(svc)
        result = svc.backfill_usage(
            api_name=API, caller=CALLER, occurred_at=OCCURRED_AT,
            call_count=CALL_COUNT, data_rows=DATA_ROWS, amount=AMOUNT,
            current_cycle=NOV, source_ref="CALL-20261031-77")
        svc.review_late_adjustment(result["adjustment"]["adjustment_id"], confirm=True)

        items = svc.list_late_adjustments(occurrence_cycle=OCT)
        assert len(items) == 1
        trace = items[0]
        assert trace["occurrence_cycle"] == OCT
        assert trace["applied_cycle"] == NOV
        assert trace["origin_voucher_no"] == oct_voucher["voucher_no"]
        assert trace["source_ref"] == "CALL-20261031-77"
        assert trace["delta_amount"] == AMOUNT

        # 对账单内嵌调整明细，可直接对账
        report = svc.reconcile_cycles(OCT, NOV)
        nov_report = next(r for r in report["cycle_reports"] if r["billing_cycle"] == NOV)
        assert len(nov_report["adjustments"]) == 1
