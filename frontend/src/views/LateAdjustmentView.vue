<template>
  <section class="page" data-module="late-adjustment">
    <header class="page-head">
      <div>
        <h2>迟到用量调整与账期对账</h2>
        <p class="page-desc">
          补到的调用按期判定结算口径：原账期未确认则当期更正并入，已确认则跨期调整差额；
          同笔明细幂等去重，重放不重复计费，沿调整明细可追溯实际发生月、归入账期与原结算凭证。
        </p>
      </div>
    </header>

    <article class="panel">
      <h3>补录迟到调用</h3>
      <form class="backfill-form" @submit.prevent="submitBackfill">
        <label class="create-field">
          <span>API名称</span>
          <input v-model="form.api_name" data-testid="backfill-api" required />
        </label>
        <label class="create-field">
          <span>调用方</span>
          <input v-model="form.caller" data-testid="backfill-caller" required />
        </label>
        <label class="create-field">
          <span>实际发生时间</span>
          <input v-model="form.occurred_at" data-testid="backfill-occurred-at"
                 placeholder="2026-10-31T23:55:00+08:00" required />
        </label>
        <label class="create-field">
          <span>调用次数</span>
          <input v-model.number="form.call_count" data-testid="backfill-count" type="number" min="1" required />
        </label>
        <label class="create-field">
          <span>数据行数</span>
          <input v-model.number="form.data_rows" data-testid="backfill-rows" type="number" min="0" />
        </label>
        <label class="create-field">
          <span>费用金额</span>
          <input v-model.number="form.amount" data-testid="backfill-amount" type="number" step="0.01" min="0" />
        </label>
        <label class="create-field">
          <span>补录时账期</span>
          <input v-model="form.current_cycle" data-testid="backfill-current-cycle"
                 placeholder="2026-11（留空取当前月）" />
        </label>
        <label class="create-field">
          <span>来源凭证</span>
          <input v-model="form.source_ref" data-testid="backfill-source" placeholder="如 CALL-20261031-77" />
        </label>
        <div class="page-actions">
          <button class="btn primary" data-testid="submit-backfill" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '判定口径并登记' }}
          </button>
        </div>
      </form>
      <p v-if="lastResult" class="scope-box" :data-kind="lastResult.adjustment_kind">
        <strong>{{ lastResult.replayed ? '幂等命中（重放未重复计费）' : '已登记调整' }}
          · {{ lastResult.adjustment_kind }}</strong>
        <span>{{ lastResult.scope_explanation }}</span>
      </p>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    </article>

    <article class="panel">
      <h3>迟到用量调整明细</h3>
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>实际发生月</span>
          <input v-model="filters.occurrence_cycle" data-testid="filter-occurrence" placeholder="2026-10" />
        </label>
        <label class="filter-item">
          <span>归入账期</span>
          <input v-model="filters.applied_cycle" data-testid="filter-applied" placeholder="2026-11" />
        </label>
        <label class="filter-item">
          <span>复核状态</span>
          <input v-model="filters.review_status" data-testid="filter-review" placeholder="待复核/已确认/已驳回" />
        </label>
        <button class="btn" data-testid="query-adjustments" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>调整编号</th>
            <th>调用方 / API</th>
            <th>差额(次/行/元)</th>
            <th>实际发生月</th>
            <th>归入账期</th>
            <th>适用口径</th>
            <th>原结算凭证</th>
            <th>入账凭证</th>
            <th>复核状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in adjustments" :key="row.adjustment_id"
              :data-testid="`adj-row-${row.adjustment_id}`" :data-status="row.review_status">
            <td>{{ row.adjustment_no }}</td>
            <td>{{ row.caller }} / {{ row.api_name }}</td>
            <td>{{ row.delta_calls }} / {{ row.data_rows }} / {{ row.delta_amount }}</td>
            <td>{{ row.occurrence_cycle }}</td>
            <td>{{ row.applied_cycle }}</td>
            <td>{{ row.adjustment_kind }}</td>
            <td>{{ row.origin_voucher_no ?? '—' }}</td>
            <td>{{ row.settlement_voucher_no ?? '—' }}</td>
            <td>{{ row.review_status }}</td>
            <td class="row-actions">
              <button v-if="row.review_status === '待复核'" class="link" type="button"
                      data-testid="confirm-review"
                      @click="review(row.adjustment_id, true)">复核确认</button>
              <button v-if="row.review_status === '待复核'" class="link" type="button"
                      data-testid="reject-review"
                      @click="review(row.adjustment_id, false)">驳回</button>
              <span v-else class="muted">{{ row.reviewer ?? '' }}</span>
            </td>
          </tr>
          <tr v-if="!adjustments.length">
            <td colspan="10" class="empty-state">暂无迟到调整明细</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <h3>跨期对账</h3>
      <form class="filter-bar" @submit.prevent="runReconcile">
        <label class="filter-item">
          <span>账期（逗号分隔）</span>
          <input v-model="reconcileInput" data-testid="reconcile-cycles"
                 placeholder="2026-10,2026-11" required />
        </label>
        <button class="btn" data-testid="run-reconcile" type="submit">生成对账单</button>
      </form>
      <div v-if="reconciliation" class="reconcile-summary">
        <p>
          两月最终合计：<strong data-testid="combined-amount">{{ reconciliation.combined_final_amount }}</strong> 元
          ／ {{ reconciliation.combined_final_calls }} 次；
          去重后已确认调整 <strong>{{ reconciliation.unique_confirmed_adjustments }}</strong> 笔，
          迟到差额合计 {{ reconciliation.combined_late_delta_amount }} 元
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>账期</th>
              <th>结算凭证</th>
              <th>原账单(元/次)</th>
              <th>迟到差额(元/次)</th>
              <th>最终口径(元/次)</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="report in reconciliation.cycle_reports" :key="report.billing_cycle"
                :data-testid="`recon-row-${report.billing_cycle}`">
              <td>{{ report.billing_cycle }}</td>
              <td>{{ report.voucher_no ?? '未结算' }}</td>
              <td>{{ report.original_amount }} / {{ report.original_calls }}</td>
              <td>{{ report.late_delta_amount }} / {{ report.late_delta_calls }}</td>
              <td>{{ report.final_amount }} / {{ report.final_calls }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  backfillUsage,
  listLateAdjustments,
  reconcileCycles,
  reviewLateAdjustment,
  type BackfillResult,
  type LateAdjustment,
  type ReconciliationResult,
} from '@/api/billing-service'

const form = ref({
  api_name: '',
  caller: '',
  occurred_at: '',
  call_count: 1,
  data_rows: 0,
  amount: 0,
  current_cycle: '',
  source_ref: '',
})
const filters = ref({ occurrence_cycle: '', applied_cycle: '', review_status: '' })
const adjustments = ref<LateAdjustment[]>([])
const reconciliation = ref<ReconciliationResult | null>(null)
const reconcileInput = ref('2026-10,2026-11')
const lastResult = ref<BackfillResult | null>(null)
const errorMessage = ref('')
const submitting = ref(false)

async function submitBackfill() {
  submitting.value = true
  errorMessage.value = ''
  try {
    lastResult.value = await backfillUsage({
      ...form.value,
      current_cycle: form.value.current_cycle || undefined,
      source_ref: form.value.source_ref || undefined,
    })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迟到用量登记失败'
  } finally {
    submitting.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listLateAdjustments({
      occurrence_cycle: filters.value.occurrence_cycle || undefined,
      applied_cycle: filters.value.applied_cycle || undefined,
      review_status: filters.value.review_status || undefined,
    })
    adjustments.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调整明细读取失败'
  }
}

async function review(adjustmentId: number, confirm: boolean) {
  errorMessage.value = ''
  try {
    await reviewLateAdjustment(adjustmentId, confirm)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核失败'
  }
}

async function runReconcile() {
  errorMessage.value = ''
  try {
    const cycles = reconcileInput.value.split(',').map((item) => item.trim()).filter(Boolean)
    reconciliation.value = await reconcileCycles(cycles)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账失败'
  }
}

onMounted(reload)
</script>
