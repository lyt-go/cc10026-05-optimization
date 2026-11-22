import { request } from './client'

/** 迟到用量调整明细（与后端 data_manager 字段对齐） */
export type LateAdjustment = {
  adjustment_id: number
  adjustment_no: string
  idem_key: string
  caller: string
  api_name: string
  call_count: number
  data_rows: number
  amount: number
  delta_calls: number
  delta_amount: number
  occurrence_cycle: string
  applied_cycle: string
  adjustment_kind: string
  origin_voucher_no: string | null
  settlement_voucher_no: string | null
  source_ref: string
  review_status: string
  reviewer: string | null
  review_remark: string
  applied: boolean
  backfilled_at: string
  reviewed_at: string | null
}

export type BackfillPayload = {
  api_name: string
  caller: string
  occurred_at: string
  call_count: number
  data_rows: number
  amount: number
  current_cycle?: string
  source_ref?: string
  remark?: string
}

export type BackfillResult = {
  ok: boolean
  replayed: boolean
  created: boolean
  adjustment_kind: string
  scope_explanation: string
  adjustment: LateAdjustment
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function backfillUsage(payload: BackfillPayload): Promise<BackfillResult> {
  const response = await request('/api/usage-billing/backfill', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<BackfillResult>(response)
}

export async function listLateAdjustments(filters: {
  occurrence_cycle?: string
  applied_cycle?: string
  review_status?: string
} = {}): Promise<{ items: LateAdjustment[]; total: number }> {
  const query = new URLSearchParams(
    Object.entries(filters).filter(([, value]) => Boolean(value)) as [string, string][],
  ).toString()
  const response = await request(`/api/usage-billing/late-adjustments?${query}`)
  return asJson(response)
}

export async function reviewLateAdjustment(
  adjustmentId: number,
  confirm: boolean,
  reviewer = '复核员',
  remark = '',
): Promise<{ ok: boolean; entry: LateAdjustment }> {
  const response = await request(
    `/api/usage-billing/late-adjustments/${adjustmentId}/review`,
    {
      method: 'POST',
      body: JSON.stringify({ confirm, reviewer, remark }),
    },
  )
  return asJson(response)
}

export type CycleReconciliation = {
  billing_cycle: string
  settled: boolean
  voucher_no: string | null
  original_amount: number
  original_calls: number
  late_delta_amount: number
  late_delta_calls: number
  final_amount: number
  final_calls: number
  adjustments: LateAdjustment[]
}

export type ReconciliationResult = {
  cycles: string[]
  cycle_reports: CycleReconciliation[]
  combined_final_amount: number
  combined_final_calls: number
  unique_confirmed_adjustments: number
  combined_late_delta_amount: number
  combined_late_delta_calls: number
}

export async function reconcileCycles(cycles: string[]): Promise<ReconciliationResult> {
  const response = await request(
    `/api/usage-billing/reconciliation?cycles=${encodeURIComponent(cycles.join(','))}`,
  )
  return asJson(response)
}
