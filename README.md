# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 迟到用量调整与账期对账

补到的调用明细（如十月最后一晚的请求十一月才补到）由 `usage_billing` 服务的
`backfill_usage` 接收，按期判定结算口径并产出可对账的迟到用量调整明细
（`data/late_adjustments/`，幂等键去重，重放不重复计费）：

- **当期更正**：实际发生月账期尚未确认，用量并入原账期账单，随该账期一起结算；
  调整明细在复核后回链结算凭证，不产生凭证外差额（避免双重计费）。
- **跨期调整**：实际发生月账期已确认（结算凭证 `frozen=true` 不可变），原单与
  历史明细一律不改，差额经复核确认后计入当前归入账期，调整明细保留
  `origin_voucher_no`（原结算凭证）与 `settlement_voucher_no`（入账凭证）闭合追溯链。
- 结算凭证（`data/usage/_settlement_<账期>.json`）冻结后重复结算返回同一凭证，
  `original_*` 永不改写，`total_* = original_* + 跨期迟到差额`。
- 对账接口沿调整明细可查实际发生月（`occurrence_cycle`）、当前归入账期
  （`applied_cycle`）与原结算凭证；跨账期合计按幂等键去重，同笔只记一次。

专用接口（`backend/app/routers/billing.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/usage-billing/backfill` | 补录迟到调用，返回口径判定与说明 |
| GET | `/api/usage-billing/late-adjustments` | 调整明细查询（发生月/归入账期/复核状态） |
| POST | `/api/usage-billing/late-adjustments/{id}/review` | 复核确认/驳回 |
| GET | `/api/usage-billing/vouchers/{cycle}` | 账期结算凭证（原账单+迟到差额+最终口径） |
| GET | `/api/usage-billing/reconciliation?cycles=2026-10,2026-11` | 跨期对账单 |

前端「迟到调整对账」页（`/usage_billing/late-adjustments`）支持补录、复核与
跨期对账；规则测试见 `backend/tests/test_late_usage_adjustment.py`。

领域：数据工程
子方向：数据分析与数据应用
