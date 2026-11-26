<template>
  <section class="detail-page" data-testid="estimate-detail-page" :data-estimate-ref="estimateRef">
    <div class="detail-head">
      <div>
        <RouterLink to="/ts_store" class="link" data-testid="back-to-store">← 返回时序存储</RouterLink>
        <h2>保留估算单详情 <span data-testid="detail-no">{{ estimate?.estimate_no ?? estimateRef }}</span></h2>
        <p class="flow-tip">已确认的估算始终可以查看当时的输入和值；后续操作以服务端当前测点参数校验一致性。</p>
      </div>
      <div class="head-actions">
        <button class="btn" type="button" data-testid="detail-reload" @click="loadAll">刷新</button>
      </div>
    </div>

    <p v-if="loading" data-testid="detail-loading">加载中…</p>
    <p v-if="error" class="flow-error" role="alert" data-testid="detail-error">{{ error }}</p>
    <p v-if="notice" class="flow-ok" role="status" data-testid="detail-message">{{ notice }}</p>

    <div v-if="conflict" class="conflict-banner" role="alert" data-testid="detail-conflict">
      <strong>测点参数已更新，当前操作缺少一致依据</strong>
      <p data-testid="detail-conflict-message">{{ conflict.message }}</p>
      <p>
        服务端有效依据：
        <strong data-testid="detail-conflict-basis">{{ basisText(conflict.server_basis) }}</strong>
      </p>
      <p v-if="conflict.pending_item">
        可续办待核项：
        <strong data-testid="detail-pending-no">{{ conflict.pending_item['待核编号'] }}</strong>
        （{{ conflict.pending_item['变更说明'] }}，状态 {{ conflict.pending_item.status }}）
      </p>
      <div class="row-actions">
        <button class="btn primary" type="button" data-testid="detail-renew" :disabled="busy" @click="renew">
          续办待核项（按当前参数重新测算）
        </button>
      </div>
    </div>

    <template v-if="estimate">
      <div class="detail-grid">
        <article class="detail-card" data-testid="detail-snapshot">
          <h3>当时的输入（冻结快照）</h3>
          <dl class="kv">
            <div><dt>单据编号</dt><dd>{{ estimate.estimate_no }}</dd></div>
            <div><dt>测点编号</dt><dd data-testid="detail-point">{{ estimate['测点编号'] }}</dd></div>
            <div>
              <dt>采样间隔</dt>
              <dd data-testid="detail-interval">
                {{ estimate['采样间隔'] }} / {{ estimate['采样间隔秒数'] }} 秒
              </dd>
            </div>
            <div><dt>策略期限</dt><dd data-testid="detail-retention">{{ estimate['策略期限'] ?? (estimate.retention_days + '天') }}</dd></div>
            <div><dt>压缩算法 / 比</dt><dd>{{ estimate['压缩算法'] }} / {{ estimate['压缩比'] }}</dd></div>
            <div><dt>存储引擎</dt><dd>{{ estimate['存储引擎'] }}</dd></div>
            <div><dt>容量口径</dt><dd data-testid="detail-basis-formula">{{ estimate['容量口径'] }}</dd></div>
            <div><dt>轮次 / 来源单</dt><dd>第 {{ estimate.round }} 轮<template v-if="estimate.source_estimate_no"> · 来源 {{ estimate.source_estimate_no }}</template></dd></div>
            <div><dt>状态</dt><dd data-testid="detail-status">{{ estimate.status }}</dd></div>
          </dl>
        </article>

        <article class="detail-card">
          <h3>当时的估算值</h3>
          <dl class="kv">
            <div><dt>每日数据点</dt><dd data-testid="detail-daily">{{ estimate['每日数据点'] }}</dd></div>
            <div><dt>原始容量</dt><dd data-testid="detail-raw">{{ estimate.raw_bytes_estimate }} 字节（{{ estimate.estimated_raw_mb }} MiB）</dd></div>
            <div><dt>压缩后容量</dt><dd data-testid="detail-compressed">{{ estimate.compressed_bytes_estimate }} 字节（{{ estimate.estimated_compressed_mb }} MiB）</dd></div>
            <div><dt>热存储</dt><dd>{{ formatBytes(estimate.hot_storage_bytes) }}</dd></div>
            <div><dt>冷存储</dt><dd>{{ formatBytes(estimate.cold_storage_bytes) }}</dd></div>
          </dl>
        </article>

        <article class="detail-card" data-testid="detail-server-basis">
          <h3>服务端有效依据（当前测点参数）</h3>
          <dl class="kv" v-if="serverBasis">
            <div><dt>当前采样间隔</dt><dd data-testid="detail-current-interval">{{ serverBasis['采样间隔'] }} / {{ serverBasis.sampling_interval_seconds }} 秒</dd></div>
            <div><dt>参数版本</dt><dd>{{ serverBasis.param_version }}</dd></div>
            <div><dt>是否与快照一致</dt><dd :data-testid="detailFresh ? 'detail-fresh' : 'detail-stale'">
              {{ detailFresh ? '一致，可继续开通' : '不一致，需续办待核项' }}
            </dd></div>
          </dl>
          <p v-else>测点不存在或依据不可用。</p>
          <form class="inline-form" @submit.prevent="updateSampling">
            <label>
              <span>修订采样间隔</span>
              <input v-model="newInterval" data-testid="detail-sampling-input" placeholder="30s" />
            </label>
            <button class="btn" type="submit" data-testid="detail-sampling-save" :disabled="busy">保存参数</button>
          </form>
        </article>
      </div>

      <div class="detail-actions" data-testid="detail-actions">
        <button class="btn" type="button" data-testid="detail-check" :disabled="busy" @click="checkOnly">核对容量</button>
        <button class="btn" type="button" data-testid="detail-confirm" :disabled="busy" @click="confirm(false)">核对容量并生成容量确认单</button>
        <button class="btn ghost" type="button" data-testid="detail-confirm-historical" :disabled="busy" @click="confirm(true)">按当时口径留存历史确认单</button>
        <button class="btn primary" type="button" data-testid="detail-transfer" :disabled="busy" @click="transfer">转办策略（取容量确认单）</button>
        <button class="btn primary" type="button" data-testid="detail-activate" :disabled="busy" @click="activate(false)">确认开通</button>
        <button class="btn" type="button" data-testid="detail-replay" :disabled="busy" @click="activate(true)">失联后重新提交同一份开通申请</button>
        <button v-if="estimate.renewable || estimate.status === '待核'" class="btn warn" type="button" data-testid="detail-action-renew" :disabled="busy" @click="renew">
          续办待核项
        </button>
      </div>

      <p v-if="checkResult" class="check-line" data-testid="detail-check-result">
        容量核对：{{ checkResult.message }}；需求 {{ formatBytes(checkResult.capacity_check?.required_bytes) }} /
        可用 {{ formatBytes(checkResult.capacity_check?.available_bytes) }}
      </p>

      <div class="detail-grid two">
        <article class="detail-card" data-testid="detail-confirmation">
          <h3>容量确认单</h3>
          <div v-if="confirmation">
            <dl class="kv">
              <div><dt>确认单号</dt><dd data-testid="detail-confirmation-no">{{ confirmation.confirmation_no }}</dd></div>
              <div><dt>显示间隔</dt><dd data-testid="detail-confirmation-interval">{{ confirmation['采样间隔'] }} / {{ confirmation['采样间隔秒数'] }} 秒</dd></div>
              <div><dt>每日数据点</dt><dd>{{ confirmation['每日数据点'] }}</dd></div>
              <div><dt>原始容量</dt><dd>{{ formatBytes(confirmation.raw_bytes_estimate) }}</dd></div>
              <div><dt>口径状态</dt><dd>{{ confirmation['口径状态'] }}</dd></div>
              <div><dt>状态</dt><dd>{{ confirmation.status }}</dd></div>
            </dl>
          </div>
          <p v-else>尚未生成容量确认单。</p>
        </article>

        <article class="detail-card" data-testid="detail-strategy">
          <h3>已确认的存储策略（幂等）</h3>
          <div v-if="strategy">
            <dl class="kv">
              <div><dt>策略编号</dt><dd data-testid="detail-strategy-no">{{ strategy['策略编号'] }}</dd></div>
              <div><dt>状态</dt><dd data-testid="detail-strategy-status">{{ strategy.status }}</dd></div>
              <div><dt>采样间隔</dt><dd data-testid="detail-strategy-interval">{{ strategy['采样间隔'] }} / {{ strategy['采样间隔秒数'] }} 秒</dd></div>
              <div><dt>每日数据点</dt><dd>{{ strategy['每日数据点'] }}</dd></div>
              <div><dt>保留天数</dt><dd>{{ strategy['保留天数'] }}</dd></div>
              <div><dt>来源估算单</dt><dd>{{ strategy['来源估算单'] }}</dd></div>
            </dl>
            <p v-if="lastDuplicate" class="flow-ok" data-testid="detail-idempotent-note">{{ lastDuplicate }}</p>
          </div>
          <p v-else>尚未确认存储策略；重复提交同一份估算只会返回同一份策略。</p>
        </article>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import {
  activateEstimate,
  checkCapacity as requestCapacityCheck,
  confirmEstimate,
  formatBytes,
  getEstimate,
  getPointBasis,
  renewEstimate,
  retentionOverview,
  transferEstimate,
  updateSampling as saveSamplingRequest,
  type FlowRecord,
} from '@/api/retention'

const props = defineProps<{ estimateRef: string }>()
const router = useRouter()

const estimate = ref<FlowRecord | null>(null)
const confirmation = ref<FlowRecord | null>(null)
const strategy = ref<FlowRecord | null>(null)
const serverBasis = ref<FlowRecord | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const conflict = ref<FlowRecord | null>(null)
const checkResult = ref<FlowRecord | null>(null)
const lastDuplicate = ref('')
const newInterval = ref('30s')

const detailFresh = computed(() => {
  if (!estimate.value || !serverBasis.value) return false
  return Number(estimate.value.sampling_interval_seconds)
    === Number(serverBasis.value.sampling_interval_seconds)
})

function basisText(basis: FlowRecord | null | undefined): string {
  if (!basis) return '服务端未返回依据'
  return `${basis['采样间隔'] ?? basis.sampling_interval}（${basis.sampling_interval_seconds} 秒，参数版本 ${basis.param_version ?? 0}）`
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const result = await getEstimate(props.estimateRef)
    if (!result.ok) throw new Error(result.message || '估算单不存在')
    const est = result.estimate as FlowRecord
    estimate.value = est

    const pointId = est['测点编号']
    const overview = await retentionOverview(pointId)
    confirmation.value = (overview.confirmations ?? []).find(
      (c: FlowRecord) => c.estimate_no === est.estimate_no,
    ) ?? null
    strategy.value = (overview.strategies ?? []).find(
      (s: FlowRecord) => s['来源估算单'] === est.estimate_no,
    ) ?? null

    // 服务端当前依据
    const basisPayload = await getPointBasis(pointId)
    serverBasis.value = basisPayload.basis ?? basisPayload.server_basis ?? null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '详情读取失败'
  } finally {
    loading.value = false
  }
}

function applyConflict(result: FlowRecord): boolean {
  if (result.conflict) {
    conflict.value = result
    error.value = result.message
    return true
  }
  conflict.value = null
  return false
}

async function checkOnly() {
  // 复用 confirm 接口的前置核对能力：这里直接请求 check 端点
  busy.value = true
  error.value = ''
  try {
    checkResult.value = await requestCapacityCheck(props.estimateRef)
  } finally {
    busy.value = false
  }
}

async function confirm(historical: boolean) {
  busy.value = true
  error.value = ''
  try {
    const result = await confirmEstimate(props.estimateRef, { allow_historical: historical })
    if (applyConflict(result)) return
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await loadAll()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '确认失败'
  } finally {
    busy.value = false
  }
}

async function transfer() {
  busy.value = true
  error.value = ''
  try {
    const result = await transferEstimate(props.estimateRef)
    if (applyConflict(result)) return
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await loadAll()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '转办失败'
  } finally {
    busy.value = false
  }
}

async function activate(isReplay: boolean) {
  busy.value = true
  error.value = ''
  lastDuplicate.value = ''
  try {
    const result = await activateEstimate(props.estimateRef)
    if (applyConflict(result)) return
    if (!result.ok) throw new Error(result.message)
    if (result.duplicate) {
      lastDuplicate.value = result.message
      notice.value = isReplay ? `重新提交结果：${result.message}` : result.message
    } else {
      notice.value = result.message
    }
    await loadAll()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '开通失败'
  } finally {
    busy.value = false
  }
}

async function renew() {
  busy.value = true
  error.value = ''
  try {
    const result = await renewEstimate(props.estimateRef)
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    conflict.value = null
    // 续办产生新单号，跳到新单详情，旧单仍可返回查看
    if (result.estimate_no) {
      await router.replace(`/ts_store/estimates/${result.estimate_no}`)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '续办失败'
  } finally {
    busy.value = false
  }
}

async function updateSampling() {
  if (!estimate.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await saveSamplingRequest(estimate.value['测点编号'], newInterval.value.trim())
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await loadAll()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '采样参数保存失败'
  } finally {
    busy.value = false
  }
}

watch(() => props.estimateRef, loadAll)
onMounted(loadAll)
</script>

<style scoped>
.detail-page { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; }
.detail-head h2 { margin: 6px 0 4px; }
.flow-tip { color: var(--muted); font-size: 12.5px; margin: 0 0 10px; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin: 12px 0; }
.detail-grid.two { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.detail-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; background: #f8fafc; }
.detail-card h3 { margin: 0 0 8px; font-size: 14px; }
.kv { margin: 0; display: flex; flex-direction: column; gap: 6px; }
.kv > div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; text-align: right; font-weight: 600; word-break: break-all; }
.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; padding: 12px; background: #f1f5f9; border-radius: 8px; }
.btn.warn { background: #b45309; border-color: #b45309; color: #fff; }
.conflict-banner { border: 1px solid #f59e0b; background: #fffbeb; border-radius: 8px; padding: 10px 12px; margin: 10px 0; }
.conflict-banner p { margin: 4px 0; font-size: 13px; }
.flow-ok { color: #15803d; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.flow-error { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.check-line { font-size: 13px; color: #334155; }
.inline-form { display: flex; gap: 8px; align-items: flex-end; margin-top: 10px; }
.inline-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.inline-form input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.row-actions { display: flex; gap: 8px; margin-top: 6px; }
</style>
