<template>
  <section class="flow-panel" data-testid="retention-workspace">
    <header class="flow-head">
      <h3>保留估算 · 容量确认 · 策略开通</h3>
      <p class="flow-tip">
        按测点当前真实采样间隔、策略期限与统一容量口径生成估算单；估算单与容量确认单都会冻结当时的输入和值，
        采样参数事后变更不会被混算，开通按估算单号幂等确认。
      </p>
    </header>

    <!-- 申请保留估算单 -->
    <form class="flow-form estimate-form" data-testid="estimate-form" @submit.prevent="submitEstimate">
      <label class="flow-field">
        <span>测点编号</span>
        <input
          v-model="form.point_id"
          list="workspace-point-options"
          data-testid="estimate-point"
          placeholder="选择或输入测点编号，如 TS_P-0001"
          required
        />
        <datalist id="workspace-point-options">
          <option v-for="point in points" :key="String(point.id)" :value="String(point['测点编号'])">
            {{ intervalText(point) }}
          </option>
        </datalist>
      </label>
      <label class="flow-field narrow">
        <span>策略期限 / 保留天数</span>
        <input
          v-model="form.retention_days"
          type="number"
          min="1"
          data-testid="estimate-retention-days"
          required
        />
      </label>
      <label class="flow-field narrow">
        <span>压缩算法（容量口径）</span>
        <select v-model="form.compression_algorithm" data-testid="estimate-compression">
          <option value="gzip">gzip（压缩比 5）</option>
          <option value="zstd">zstd（压缩比 6）</option>
          <option value="lz4">lz4（压缩比 3）</option>
          <option value="snappy">snappy（压缩比 2）</option>
          <option value="none">none（不压缩）</option>
        </select>
      </label>
      <label class="flow-field narrow">
        <span>存储引擎</span>
        <input v-model="form.storage_engine" data-testid="estimate-engine" placeholder="tsdb" />
      </label>
      <button class="btn primary" type="submit" data-testid="submit-estimate" :disabled="busy">
        {{ busy ? '测算中…' : '申请保留估算单' }}
      </button>
    </form>

    <!-- 采样参数修订（制造/处理 10s -> 30s 冲突） -->
    <form class="flow-form sampling-inline" data-testid="workspace-sampling-form" @submit.prevent="submitSampling">
      <span class="basis-label">
        服务端有效依据 · 当前采样间隔：
        <strong data-testid="current-basis-interval">{{ selectedBasisText }}</strong>
      </span>
      <label class="flow-field narrow">
        <span>修订采样间隔（秒 / 10s / 30s）</span>
        <input v-model="samplingInterval" data-testid="workspace-sampling-interval" placeholder="30" />
      </label>
      <button class="btn" type="submit" data-testid="workspace-update-sampling" :disabled="busy">
        修订采样参数
      </button>
      <button class="btn ghost" type="button" data-testid="refresh-flow" @click="refreshAll">刷新单据</button>
    </form>

    <p v-if="notice" class="flow-ok" role="status" data-testid="flow-message">{{ notice }}</p>
    <p v-if="error" class="flow-error" role="alert" data-testid="flow-error">{{ error }}</p>

    <!-- 冲突：服务端有效依据 + 可续办待核项 -->
    <div v-if="conflict" class="conflict-banner" role="alert" data-testid="conflict-banner">
      <strong>采样参数与开通口径冲突</strong>
      <p data-testid="conflict-message">{{ conflict.message }}</p>
      <p>
        服务端有效依据（当前采样间隔）：
        <strong data-testid="conflict-basis">{{ basisText(conflict.server_basis) }}</strong>
      </p>
      <p v-if="conflict.pending_item">
        已生成可续办待核项
        <strong data-testid="conflict-pending-no">{{ conflict.pending_item['待核编号'] }}</strong>
        ：{{ conflict.pending_item['变更说明'] }}（状态：{{ conflict.pending_item.status }}）
      </p>
      <div class="row-actions">
        <button
          class="btn primary"
          type="button"
          data-testid="conflict-renew"
          :disabled="busy"
          @click="renew(conflict.estimate_no || conflict.estimate?.estimate_no)"
        >
          按当前参数续办 / 重新测算
        </button>
        <button class="btn ghost" type="button" data-testid="conflict-dismiss" @click="conflict = null">
          知道了
        </button>
        <button
          class="btn"
          type="button"
          :data-testid="`conflict-open-detail`"
          @click="goDetail(conflict.estimate_no || conflict.estimate?.estimate_no)"
        >
          打开估算单详情
        </button>
      </div>
    </div>

    <div class="flow-grid">
      <!-- 保留估算单 -->
      <div class="flow-block">
        <div class="block-head">
          <h4>保留估算单</h4>
          <button class="link" type="button" data-testid="new-independent-estimate" @click="focusForm">
            发起另一次独立测算
          </button>
        </div>
        <table class="data-table flow-table" data-testid="estimate-table">
          <thead>
            <tr>
              <th>单号</th>
              <th>测点</th>
              <th>采样间隔</th>
              <th>策略期限</th>
              <th>每日数据点</th>
              <th>压缩后容量</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in estimates"
              :key="String(row.id)"
              :data-testid="`estimate-row-${row.id}`"
              :data-estimate-no="row.estimate_no"
            >
              <td>
                <RouterLink
                  class="link"
                  :to="`/ts_store/estimates/${row.estimate_no}`"
                  :data-testid="`estimate-row-${row.estimate_no}`"
                >
                  {{ row.estimate_no }}
                </RouterLink>
              </td>
              <td>{{ row['测点编号'] }}</td>
              <td :data-testid="`estimate-interval-${row.id}`">{{ row['采样间隔'] }}（{{ row['采样间隔秒数'] }}秒）</td>
              <td>{{ row['策略期限'] ?? `${row.retention_days}天` }}</td>
              <td :data-testid="`estimate-daily-${row.id}`">{{ row['每日数据点'] }}</td>
              <td>{{ formatBytes(row['compressed_bytes_estimate']) }}</td>
              <td :data-testid="`estimate-status-${row.id}`">{{ row.status }}</td>
              <td class="row-actions-cell">
                <button
                  class="link"
                  type="button"
                  :data-testid="`action-estimate-detail-${row.id}`"
                  @click="goDetail(row.estimate_no)"
                >
                  详情
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`action-estimate-transfer-${row.id}`"
                  @click="transfer(row.estimate_no)"
                >
                  核对容量/转办策略
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`action-estimate-activate-${row.id}`"
                  @click="activate(row.estimate_no)"
                >
                  确认开通
                </button>
                <button
                  v-if="row.renewable || row.status === '待核'"
                  class="link warn"
                  type="button"
                  :data-testid="`action-estimate-renew-${row.id}`"
                  @click="renew(row.estimate_no)"
                >
                  续办
                </button>
              </td>
            </tr>
            <tr v-if="!estimates.length">
              <td colspan="8" class="empty-state">还没有保留估算单，先在上方申请一轮测算</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 容量确认单（历史） -->
      <div class="flow-block">
        <h4>历史容量确认单（冻结当时间隔与估算值）</h4>
        <table class="data-table flow-table" data-testid="confirmation-table">
          <thead>
            <tr>
              <th>确认单号</th>
              <th>来源估算单</th>
              <th>测点</th>
              <th>显示间隔</th>
              <th>每日数据点</th>
              <th>策略期限</th>
              <th>原始容量</th>
              <th>口径</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in confirmations"
              :key="String(row.id)"
              :data-testid="`confirmation-row-${row.id}`"
              :data-confirmation-no="row.confirmation_no"
            >
              <td :data-testid="`confirmation-row-${row.confirmation_no}`">{{ row.confirmation_no }}</td>
              <td>{{ row.estimate_no }}</td>
              <td>{{ row['测点编号'] }}</td>
              <td :data-testid="`confirmation-interval-${row.id}`">
                {{ row['采样间隔'] }}（{{ row['采样间隔秒数'] }}秒）
              </td>
              <td :data-testid="`confirmation-daily-${row.id}`">{{ row['每日数据点'] }}</td>
              <td>{{ row['策略期限'] ?? `${row.retention_days}天` }}</td>
              <td :data-testid="`confirmation-raw-${row.id}`">{{ formatBytes(row.raw_bytes_estimate) }}</td>
              <td>{{ row['口径状态'] }}</td>
              <td>{{ row.status }}</td>
            </tr>
            <tr v-if="!confirmations.length">
              <td colspan="9" class="empty-state">转办策略后生成容量确认单</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 当前启用的存储策略 -->
      <div class="flow-block" data-testid="current-strategy">
        <h4>当前启用的存储策略（数字对应当前显示间隔）</h4>
        <table class="data-table flow-table" data-testid="strategy-snapshot-table">
          <thead>
            <tr>
              <th>策略编号</th>
              <th>测点</th>
              <th>采样间隔</th>
              <th>每日数据点</th>
              <th>保留天数</th>
              <th>压缩后容量</th>
              <th>状态</th>
              <th>来源估算单</th>
              <th>幂等操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in strategies"
              :key="String(row.id)"
              :data-testid="`current-strategy-row-${row.id}`"
              :data-strategy-no="row['策略编号']"
            >
              <td>{{ row['策略编号'] }}</td>
              <td>{{ row['测点编号'] }}</td>
              <td :data-testid="`strategy-interval-${row.id}`">
                {{ row['采样间隔'] }}（{{ row['采样间隔秒数'] }}秒）
              </td>
              <td :data-testid="`strategy-daily-${row.id}`">{{ row['每日数据点'] }}</td>
              <td>{{ row['保留天数'] }}</td>
              <td>{{ formatBytes(row['compressed_bytes_estimate']) }}</td>
              <td :data-testid="`strategy-status-${row.id}`">{{ row.status }}</td>
              <td>{{ row['来源估算单'] }}</td>
              <td>
                <button
                  class="link"
                  type="button"
                  :data-testid="`strategy-replay-${row.id}`"
                  @click="activate(row['来源估算单'], true)"
                >
                  重新提交/重复领取
                </button>
              </td>
            </tr>
            <tr v-if="!strategies.length">
              <td colspan="9" class="empty-state">确认开通后在此看到当前策略，重复领取不会新增策略</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  activateEstimate,
  createEstimate,
  formatBytes,
  listFlowPoints,
  retentionOverview,
  transferEstimate,
  renewEstimate,
  updateSampling,
  type FlowRecord,
} from '@/api/retention'

const emit = defineEmits<{ (e: 'changed'): void }>()
const router = useRouter()

function goDetail(estimateNo: string) {
  if (estimateNo) void router.push(`/ts_store/estimates/${estimateNo}`)
}

const points = ref<FlowRecord[]>([])
const estimates = ref<FlowRecord[]>([])
const confirmations = ref<FlowRecord[]>([])
const strategies = ref<FlowRecord[]>([])
const busy = ref(false)
const notice = ref('')
const error = ref('')
const conflict = ref<FlowRecord | null>(null)
const samplingInterval = ref('30')

const form = ref({
  point_id: '',
  retention_days: 30,
  compression_algorithm: 'gzip',
  storage_engine: 'tsdb',
})

const selectedBasisText = computed(() => {
  if (!form.value.point_id) return '先选择/输入测点'
  const point = points.value.find((p) => String(p['测点编号']) === form.value.point_id.trim())
  if (!point) return `未在列表中找到 ${form.value.point_id}（将以服务端查询为准）`
  return intervalText(point)
})

function intervalText(point: FlowRecord): string {
  const basis = point.basis as FlowRecord | undefined
  const seconds = basis?.sampling_interval_seconds ?? point['采样间隔秒数']
  if (seconds != null) return `${seconds}s（${seconds}秒）`
  return point['采集间隔'] ? String(point['采集间隔']) : '—'
}

function basisText(basis: FlowRecord | null | undefined): string {
  if (!basis) return '服务端未返回依据'
  return `${basis['采样间隔'] ?? basis.sampling_interval}（采样间隔 ${basis.sampling_interval_seconds} 秒，参数版本 ${basis.param_version ?? 0}）`
}

function focusForm() {
  notice.value = '可在上方修改测点或期限后，再次「申请保留估算」发起独立测算'
  const input = document.querySelector<HTMLInputElement>('[data-testid="estimate-point"] input, [data-testid="estimate-point"]')
  input?.focus()
}

async function loadPoints() {
  try {
    const payload = await listFlowPoints()
    points.value = payload.items ?? []
  } catch (e) {
    error.value = e instanceof Error ? e.message : '测点读取失败'
  }
}

async function refreshAll() {
  try {
    const overview = await retentionOverview()
    estimates.value = overview.estimates ?? []
    confirmations.value = overview.confirmations ?? []
    strategies.value = (overview.strategies ?? []).filter((s: FlowRecord) => s.status === '已生效')
    await loadPoints()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '流程单据读取失败'
  }
}

function handleResult(result: FlowRecord, successPrefix = ''): boolean {
  if (result.conflict) {
    conflict.value = result
    error.value = result.message || '采样参数已变更，请先续办待核项'
    return false
  }
  if (!result.ok) {
    error.value = result.message || '操作失败'
    return false
  }
  conflict.value = null
  notice.value = [successPrefix, result.message].filter(Boolean).join(' ')
  return true
}

async function submitEstimate() {
  busy.value = true
  error.value = ''
  notice.value = ''
  conflict.value = null
  try {
    const result = await createEstimate({ ...form.value })
    if (!handleResult(result)) return
    notice.value = result.message
    form.value.point_id = result.estimate?.['测点编号'] ?? form.value.point_id
    await refreshAll()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '估算单申请失败'
  } finally {
    busy.value = false
  }
}

async function submitSampling() {
  if (!form.value.point_id) {
    error.value = '请先在「测点编号」中选择或输入测点'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await updateSampling(form.value.point_id.trim(), samplingInterval.value.trim())
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await refreshAll()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '采样参数修订失败'
  } finally {
    busy.value = false
  }
}

async function transfer(estimateNo: string) {
  busy.value = true
  error.value = ''
  try {
    const result = await transferEstimate(estimateNo)
    handleResult(result)
    await refreshAll()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '转办失败'
  } finally {
    busy.value = false
  }
}

async function activate(estimateNo: string, isReplay = false) {
  busy.value = true
  error.value = ''
  try {
    const result = await activateEstimate(estimateNo)
    if (result.duplicate) {
      notice.value = `${isReplay ? '重复领取/失联重试：' : ''}${result.message}`
      conflict.value = null
    } else {
      handleResult(result, isReplay ? '重新提交结果：' : '')
    }
    await refreshAll()
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '开通申请失败'
  } finally {
    busy.value = false
  }
}

async function renew(estimateNo: string) {
  if (!estimateNo) {
    error.value = '缺少待续办的估算单号'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await renewEstimate(estimateNo)
    if (!handleResult(result)) return
    notice.value = result.message
    conflict.value = null
    await refreshAll()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '续办失败'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await loadPoints()
  await refreshAll()
})

defineExpose({ refresh: refreshAll })
</script>

<style scoped>
.flow-panel { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 12px 0 18px; }
.flow-head h3 { margin: 0 0 4px; font-size: 16px; }
.flow-head h4, .flow-block h4 { margin: 0 0 8px; font-size: 14px; }
.flow-tip { color: var(--muted); font-size: 12.5px; margin: 0 0 10px; max-width: 980px; }
.flow-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 10px; }
.flow-field { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.flow-field.narrow { min-width: 170px; }
.flow-field input, .flow-field select { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; min-width: 210px; }
.flow-field.narrow input, .flow-field.narrow select { min-width: 160px; }
.sampling-inline { border-top: 1px dashed var(--border); padding-top: 10px; }
.basis-label { font-size: 13px; margin-right: 8px; }
.flow-ok { color: #15803d; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.flow-error { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.conflict-banner { border: 1px solid #f59e0b; background: #fffbeb; border-radius: 8px; padding: 10px 12px; margin: 8px 0 12px; }
.conflict-banner p { margin: 4px 0; font-size: 13px; }
.flow-grid { display: flex; flex-direction: column; gap: 14px; margin-top: 12px; }
.flow-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.block-head { display: flex; justify-content: space-between; align-items: center; }
.flow-table { font-size: 12.5px; }
.row-actions-cell { white-space: nowrap; }
.row-actions-cell .link { margin-right: 8px; }
.link.warn { color: #b45309; }
.row-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.empty-state { text-align: center; color: var(--muted); padding: 16px; }
</style>
