"""跨模块共享数据管理器：读写 data/ 目录下的业务数据产物。

职责：
- 每个服务产出 domain_snapshot 后序列化到 data/{module}/snapshot.json
- 下游服务从 data/ 目录读取上游的标准化数据产物
- 审计记录写入 data/audit/
- 用量记录写入 data/usage/
- 导出任务结果写入 data/exports/
- API 注册快照写入 data/api/
- 订阅审批记录写入 data/subscriptions/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据目录相对于仓库根
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(
    Path(__file__).resolve().parents[3] / "data"
)))

# 东八区
TZ = timezone(timedelta(hours=8))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# ========== API 注册快照 ==========


def write_api_snapshot(api_id: int, api_data: dict[str, Any]) -> None:
    """将单个 API 注册数据写入共享快照，供路由、缓存、限流等模块消费。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    api_data["_synced_at"] = _now_iso()
    snapshot[key] = api_data
    _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


def read_api_snapshot(api_id: int | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
    """读取 API 注册快照。返回单个或全部。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    if api_id is not None:
        return snapshot.get(str(api_id), {})
    return snapshot


def remove_api_snapshot(api_id: int) -> None:
    """标记 API 为下线。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    if key in snapshot:
        snapshot[key]["status"] = "已下线"
        snapshot[key]["_deleted_at"] = _now_iso()
        _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


# ========== 订阅审批 ==========


def write_subscription(sub_id: int, sub_data: dict[str, Any]) -> None:
    """写入订阅申请记录到共享目录。"""
    record = {
        "subscription_id": sub_id,
        **sub_data,
        "_created_at": sub_data.get("_created_at", _now_iso()),
        "_updated_at": _now_iso(),
    }
    _write_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", record)


def read_subscription(sub_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", {})


def list_subscriptions() -> list[dict[str, Any]]:
    """列出所有订阅记录。"""
    _ensure_dir(DATA_ROOT / "subscriptions")
    records = []
    for f in sorted(DATA_ROOT.glob("subscriptions/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def approve_subscription(sub_id: int, approved: bool, approver: str = "system") -> dict[str, Any]:
    """审批订阅，记录审批流程。"""
    record = read_subscription(sub_id)
    if not record:
        return {}
    record["approval_status"] = "已通过" if approved else "已驳回"
    record["approver"] = approver
    record["_approved_at"] = _now_iso()
    write_subscription(sub_id, record)
    return record


# ========== 用量计费 ==========


def write_usage(usage_id: int, usage_data: dict[str, Any]) -> None:
    """写入用量记录到共享目录。"""
    usage_data["_created_at"] = usage_data.get("_created_at", _now_iso())
    usage_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", usage_data)


def read_usage(usage_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", {})


def list_usage(billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """列出用量记录，支持按计费周期过滤。"""
    _ensure_dir(DATA_ROOT / "usage")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/*.json")):
        record = _read_json(f)
        if record:
            if billing_cycle is None or record.get("billing_cycle") == billing_cycle:
                records.append(record)
    return records


def settle_billing(billing_cycle: str, use_auto_settlement: bool = True) -> dict[str, Any]:
    """结算指定计费周期，生成不可变结算凭证。

    凭证一旦冻结（frozen=true）即代表操作员已确认的账单，后续重复结算或
    迟到用量回填都不得改写原账单金额，只能通过迟到用量调整明细追加差额。
    """
    voucher_path = DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json"
    existing = _read_json(voucher_path)
    if isinstance(existing, dict) and existing.get("frozen"):
        # 已确认的结算凭证不可重算，直接返回原凭证，保证重复结算结果一致。
        return existing

    records = list_usage(billing_cycle)
    total_amount = sum(float(r.get("amount", 0) or 0) for r in records)
    total_calls = sum(int(r.get("call_count", 0) or 0) for r in records)
    settled_at = _now_iso()
    voucher_no = f"STL-{billing_cycle}-{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
    settlement = {
        "voucher_no": voucher_no,
        "billing_cycle": billing_cycle,
        # original_*：操作员确认时的账单口径，冻结后永不改写。
        "original_amount": total_amount,
        "original_calls": total_calls,
        "record_count": len(records),
        # 迟到调整：只汇总已复核确认、且归入本账期的差额。
        "late_adjustment_amount": 0.0,
        "late_adjustment_calls": 0,
        "late_adjustment_count": 0,
        # total_*：原账单 + 已确认迟到差额，是最终对外口径。
        "total_amount": total_amount,
        "total_calls": total_calls,
        "settled_at": settled_at,
        "frozen_at": settled_at,
        "frozen": True,
        "auto_settlement": use_auto_settlement,
    }
    _write_json(voucher_path, settlement)

    for r in records:
        r["settlement_status"] = "已结算"
        r["_settled_at"] = settled_at
        r["settlement_voucher_no"] = voucher_no
        write_usage(int(r.get("usage_id", 0) or 0), r)

    # 结算提交时，把已确认的当期更正明细回链到本凭证，并刷新凭证差额汇总。
    attach_adjustments_to_voucher(billing_cycle)
    return _read_json(voucher_path, settlement)


def read_settlement_voucher(billing_cycle: str) -> dict[str, Any]:
    """读取指定账期的结算凭证；未结算时返回空字典。"""
    return _read_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", {})


def is_cycle_settled(billing_cycle: str) -> bool:
    """账期是否已确认（凭证已冻结）。冻结账期的原单与历史明细禁止改写。"""
    voucher = read_settlement_voucher(billing_cycle)
    return bool(voucher.get("frozen"))


# ========== 迟到用量调整 ==========
#
# 背景：十月最后一晚的请求十一月才补到，而十月结算可能已被操作员确认。
# 调整明细是可对账的独立产物，承载：
# - 实际发生月（occurrence_cycle）与当前归入账期（applied_cycle）
# - 原结算凭证号（origin_voucher_no），可回链到当时已确认的明细
# - 差额（delta_*）与复核口径（adjustment_kind / review_status）
# 同笔迟到明细通过幂等键（idem_key）去重，重放只返回原记录，绝不重复计费。


LATE_ADJUSTMENT_DIR = "late_adjustments"
LATE_ADJUSTMENT_INDEX = "_idempotency_index.json"


def _late_dir() -> Path:
    path = DATA_ROOT / LATE_ADJUSTMENT_DIR
    _ensure_dir(path)
    return path


def _read_idem_index() -> dict[str, str]:
    data = _read_json(_late_dir() / LATE_ADJUSTMENT_INDEX, {})
    return data if isinstance(data, dict) else {}


def _write_idem_index(index: dict[str, str]) -> None:
    _write_json(_late_dir() / LATE_ADJUSTMENT_INDEX, index)


def find_late_adjustment_by_idem(idem_key: str) -> dict[str, Any] | None:
    """按幂等键查找已登记的迟到调整，重放同一笔时复用。"""
    adjustment_id = _read_idem_index().get(idem_key)
    if not adjustment_id:
        return None
    return _read_json(_late_dir() / f"{int(adjustment_id):06d}.json", None)


def list_late_adjustments(
    occurrence_cycle: str | None = None,
    applied_cycle: str | None = None,
    review_status: str | None = None,
) -> list[dict[str, Any]]:
    """列出迟到用量调整明细，支持按发生月、归入账期、复核状态过滤。"""
    records = []
    for f in sorted(_late_dir().glob("[0-9]*.json")):
        record = _read_json(f)
        if not record:
            continue
        if occurrence_cycle and record.get("occurrence_cycle") != occurrence_cycle:
            continue
        if applied_cycle and record.get("applied_cycle") != applied_cycle:
            continue
        if review_status and record.get("review_status") != review_status:
            continue
        records.append(record)
    return records


def _next_late_adjustment_id() -> int:
    return max((int(f.stem) for f in _late_dir().glob("[0-9]*.json")), default=0) + 1


def register_late_adjustment(
    *,
    idem_key: str,
    caller: str,
    api_name: str,
    call_count: int,
    data_rows: int,
    amount: float,
    occurrence_cycle: str,
    applied_cycle: str,
    adjustment_kind: str,
    origin_voucher_no: str | None,
    source_ref: str = "",
    remark: str = "",
) -> tuple[dict[str, Any], bool]:
    """登记一笔迟到用量调整明细。

    返回 (调整明细, 是否新建)。命中幂等键时直接返回原明细（created=False），
    重放不会产生第二笔费用，两个月合计始终只记一笔。
    """
    existing = find_late_adjustment_by_idem(idem_key)
    if existing is not None:
        return existing, False

    adjustment_id = _next_late_adjustment_id()
    record = {
        "adjustment_id": adjustment_id,
        "adjustment_no": f"LADJ-{adjustment_id:06d}",
        "idem_key": idem_key,
        "caller": caller,
        "api_name": api_name,
        "call_count": int(call_count),
        "data_rows": int(data_rows),
        "amount": float(amount),
        # 差额即本笔迟到用量本身（原账期未计过），保留 delta_ 命名便于对账。
        "delta_calls": int(call_count),
        "delta_rows": int(data_rows),
        "delta_amount": float(amount),
        "occurrence_cycle": occurrence_cycle,
        "applied_cycle": applied_cycle,
        # 口径：当期更正（账期未冻结，并入原账期）/ 跨期调整（原账期已冻结）。
        "adjustment_kind": adjustment_kind,
        "origin_voucher_no": origin_voucher_no,
        "settlement_voucher_no": None,
        "source_ref": source_ref,
        "review_status": "待复核",
        "reviewer": None,
        "review_remark": remark,
        "applied": False,
        "backfilled_at": _now_iso(),
        "reviewed_at": None,
        "_created_at": _now_iso(),
        "_updated_at": _now_iso(),
    }
    _write_json(_late_dir() / f"{adjustment_id:06d}.json", record)
    index = _read_idem_index()
    index[idem_key] = str(adjustment_id)
    _write_idem_index(index)
    return record, True


def review_late_adjustment(adjustment_id: int, *, confirm: bool,
                           reviewer: str = "复核员", remark: str = "") -> dict[str, Any] | None:
    """复核迟到调整：确认后差额才允许计入结算汇总；驳回则不参与计费。"""
    path = _late_dir() / f"{int(adjustment_id):06d}.json"
    record = _read_json(path, None)
    if record is None:
        return None
    if record.get("review_status") in ("已确认", "已驳回"):
        # 复核结论幂等：重复提交不改变历史结论。
        return record
    record["review_status"] = "已确认" if confirm else "已驳回"
    record["reviewer"] = reviewer
    record["reviewed_at"] = _now_iso()
    if remark:
        record["review_remark"] = remark
    record["_updated_at"] = _now_iso()
    _write_json(path, record)

    # 确认后若归入账期已结算，立即把差额补进该账期凭证（凭证原账单金额不动）。
    if confirm and is_cycle_settled(record["applied_cycle"]):
        attach_adjustments_to_voucher(record["applied_cycle"])
    # attach 会回写凭证号，重新读取保证调用方拿到闭合后的追溯链。
    return _read_json(path, record)


def confirmed_adjustments_for_cycle(billing_cycle: str) -> list[dict[str, Any]]:
    """归入指定账期、且已复核确认的迟到调整明细。"""
    return [
        r for r in list_late_adjustments(applied_cycle=billing_cycle)
        if r.get("review_status") == "已确认"
    ]


def _cross_period_adjustments(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨期调整才形成凭证之外的差额；当期更正已并入用量行，不得重复加价。"""
    return [r for r in records if r.get("adjustment_kind") == "跨期调整"]


def attach_adjustments_to_voucher(billing_cycle: str) -> dict[str, Any]:
    """把已确认的迟到调整差额汇总进账期结算凭证。

    - 凭证的 original_amount/original_calls 保持冻结值不变。
    - 「跨期调整」差额计入 late_adjustment_* 并累加进 total_*。
    - 「当期更正」的差额已包含在原账期用量行（即 original_*）内，
      这里只回链凭证号用于追溯，不再重复加价。
    """
    voucher_path = DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json"
    voucher = _read_json(voucher_path)
    if not isinstance(voucher, dict) or not voucher.get("frozen"):
        return {}

    confirmed = confirmed_adjustments_for_cycle(billing_cycle)
    cross_period = _cross_period_adjustments(confirmed)
    delta_amount = sum(float(r.get("delta_amount", 0) or 0) for r in cross_period)
    delta_calls = sum(int(r.get("delta_calls", 0) or 0) for r in cross_period)
    voucher["late_adjustment_amount"] = delta_amount
    voucher["late_adjustment_calls"] = delta_calls
    voucher["late_adjustment_count"] = len(cross_period)
    voucher["total_amount"] = float(voucher.get("original_amount", 0) or 0) + delta_amount
    voucher["total_calls"] = int(voucher.get("original_calls", 0) or 0) + delta_calls
    voucher["_updated_at"] = _now_iso()
    _write_json(voucher_path, voucher)

    # 所有已确认调整（含当期更正）都回链凭证，闭合「调整 → 结算凭证」追溯链。
    for record in confirmed:
        path = _late_dir() / f"{int(record['adjustment_id']):06d}.json"
        record["settlement_voucher_no"] = voucher["voucher_no"]
        record["applied"] = True
        record["_updated_at"] = _now_iso()
        _write_json(path, record)
    return voucher


def build_cycle_reconciliation(billing_cycle: str) -> dict[str, Any]:
    """产出单账期对账单：原账单、迟到差额、最终口径与可追溯调整明细。"""
    voucher = read_settlement_voucher(billing_cycle)
    adjustments = list_late_adjustments(applied_cycle=billing_cycle)
    confirmed = [r for r in adjustments if r.get("review_status") == "已确认"]
    # 只有跨期调整构成凭证外差额；当期更正已在原账单用量行内。
    cross_period = _cross_period_adjustments(confirmed)
    original_amount = float(voucher.get("original_amount", 0) or 0) if voucher else 0.0
    original_calls = int(voucher.get("original_calls", 0) or 0) if voucher else 0
    delta_amount = sum(float(r.get("delta_amount", 0) or 0) for r in cross_period)
    delta_calls = sum(int(r.get("delta_calls", 0) or 0) for r in cross_period)
    return {
        "billing_cycle": billing_cycle,
        "settled": bool(voucher.get("frozen")),
        "voucher_no": voucher.get("voucher_no"),
        "original_amount": original_amount,
        "original_calls": original_calls,
        "late_delta_amount": delta_amount,
        "late_delta_calls": delta_calls,
        "final_amount": original_amount + delta_amount,
        "final_calls": original_calls + delta_calls,
        "adjustments": adjustments,
    }


# ========== 调用审计 ==========


def write_audit(audit_id: int, audit_data: dict[str, Any]) -> None:
    """写入审计记录。"""
    audit_data["_logged_at"] = _now_iso()
    _write_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", audit_data)


def read_audit(audit_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", {})


def list_audit(api_name: str | None = None, caller: str | None = None,
               start_time: str | None = None, end_time: str | None = None) -> list[dict[str, Any]]:
    """按条件查询审计记录。"""
    _ensure_dir(DATA_ROOT / "audit")
    records = []
    for f in sorted(DATA_ROOT.glob("audit/*.json")):
        record = _read_json(f)
        if not record:
            continue
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        if start_time and record.get("_logged_at", "") < start_time:
            continue
        if end_time and record.get("_logged_at", "") > end_time:
            continue
        records.append(record)
    return records


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档指定日期之前的审计记录。"""
    records = list_audit()
    archived = [r for r in records if r.get("_logged_at", "")[:10] < before_date]
    archive_data = {
        "archive_date": _now_iso(),
        "before_date": before_date,
        "archived_count": len(archived),
        "records": archived,
    }
    _write_json(DATA_ROOT / "audit" / f"_archive_{before_date}.json", archive_data)
    # 标记已归档
    for r in archived:
        rid = r.get("audit_id", 0)
        r["status"] = "已归档"
        write_audit(rid, r)
    return archive_data


# ========== 导出任务 ==========


def write_export(export_id: int, export_data: dict[str, Any]) -> None:
    """写入导出任务结果。"""
    export_data["_created_at"] = export_data.get("_created_at", _now_iso())
    export_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", export_data)
    # 同时生成一份导出 CSV
    _write_export_csv(export_id, export_data)


def _write_export_csv(export_id: int, export_data: dict[str, Any]) -> None:
    """根据导出数据生成 CSV 文件头。"""
    columns = export_data.get("columns", [])
    rows = export_data.get("rows", [])
    csv_path = DATA_ROOT / "exports" / f"{export_id:06d}.csv"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")


def read_export(export_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", {})


def list_exports() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "exports")
    records = []
    for f in sorted(DATA_ROOT.glob("exports/[0-9]*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


# ========== 模块数据快照 ==========


def write_module_snapshot(module: str, snapshot: dict[str, Any]) -> None:
    """各服务将自己的 domain_snapshot 写入共享目录，供跨模块消费。"""
    snapshot["_snapshot_at"] = _now_iso()
    _write_json(DATA_ROOT / module / "snapshot.json", snapshot)


def read_module_snapshot(module: str) -> dict[str, Any]:
    return _read_json(DATA_ROOT / module / "snapshot.json", {})


def list_module_snapshots() -> list[dict[str, Any]]:
    """读取所有模块的快照汇总。"""
    snapshots = []
    for module_dir in DATA_ROOT.iterdir():
        if module_dir.is_dir() and not module_dir.name.startswith("_"):
            snap = _read_json(module_dir / "snapshot.json")
            if snap:
                snapshots.append(snap)
    return snapshots