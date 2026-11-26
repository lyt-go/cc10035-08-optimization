<template>
  <section class="flow-panel" data-testid="point-sampling-panel">
    <header class="flow-head">
      <h3>测点采样参数（服务端有效依据）</h3>
      <p class="flow-tip">
        保留估算与容量确认都以这里的服务端采样间隔为准；把 10s 修订为 30s 后，变更前的估算单不会被混算。
      </p>
    </header>

    <form class="flow-form" data-testid="sampling-form" @submit.prevent="submitUpdate">
      <label class="flow-field">
        <span>测点编号</span>
        <input
          v-model="pointId"
          list="sampling-point-options"
          data-testid="sampling-point"
          placeholder="例如 TS_P-0001"
          required
        />
        <datalist id="sampling-point-options">
          <option v-for="point in points" :key="String(point.id)" :value="String(point['测点编号'])">
            当前 {{ intervalText(point) }}
          </option>
        </datalist>
      </label>
      <label class="flow-field narrow">
        <span>采样间隔（秒，或填 10s / 30s）</span>
        <input
          v-model="interval"
          data-testid="sampling-interval"
          placeholder="10"
          required
        />
      </label>
      <button class="btn primary" type="submit" data-testid="update-sampling" :disabled="saving">
        {{ saving ? '提交中…' : '修订采样间隔' }}
      </button>
      <button class="btn ghost" type="button" data-testid="reload-points" @click="loadPoints">
        刷新测点
      </button>
    </form>

    <p v-if="message" class="flow-ok" data-testid="sampling-message">{{ message }}</p>
    <p v-if="error" class="flow-error" data-testid="sampling-error">{{ error }}</p>

    <table class="data-table flow-table" data-testid="sampling-point-table">
      <thead>
        <tr>
          <th>测点编号</th>
          <th>测点名称</th>
          <th>当前采样间隔</th>
          <th>参数版本</th>
          <th>快捷修订</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="point in points" :key="String(point.id)" :data-testid="`sampling-row-${point.id}`">
          <td>{{ point['测点编号'] }}</td>
          <td>{{ point['测点名称'] ?? '—' }}</td>
          <td :data-testid="`sampling-current-${point.id}`">
            <strong>{{ intervalText(point) }}</strong>
          </td>
          <td>{{ point['参数版本'] ?? 0 }}</td>
          <td>
            <button
              class="link"
              type="button"
              :data-testid="`sampling-set30-${point.id}`"
              @click="quickSet(point, 30)"
            >
              调为 30s
            </button>
            &nbsp;/&nbsp;
            <button
              class="link"
              type="button"
              :data-testid="`sampling-set10-${point.id}`"
              @click="quickSet(point, 10)"
            >
              调为 10s
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listFlowPoints, updateSampling, type FlowRecord } from '@/api/retention'

const points = ref<FlowRecord[]>([])
const pointId = ref('')
const interval = ref('30')
const saving = ref(false)
const message = ref('')
const error = ref('')

function intervalText(point: FlowRecord): string {
  const basis = point.basis as FlowRecord | undefined
  const seconds = basis?.sampling_interval_seconds ?? point['采样间隔秒数']
  const raw = point['采集间隔']
  if (seconds != null) return `${seconds}s（${seconds}秒）`
  return raw ? String(raw) : '—'
}

async function loadPoints() {
  error.value = ''
  try {
    const payload = await listFlowPoints()
    points.value = payload.items ?? []
  } catch (e) {
    error.value = e instanceof Error ? e.message : '测点读取失败'
  }
}

async function submitUpdate() {
  saving.value = true
  message.value = ''
  error.value = ''
  try {
    const result = await updateSampling(pointId.value.trim(), interval.value.trim())
    if (!result.ok) throw new Error(result.message || '采样参数修订失败')
    message.value = result.message
    await loadPoints()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '采样参数修订失败'
  } finally {
    saving.value = false
  }
}

async function quickSet(point: FlowRecord, seconds: number) {
  pointId.value = String(point['测点编号'])
  interval.value = String(seconds)
  await submitUpdate()
}

onMounted(loadPoints)

defineExpose({ reload: loadPoints })
</script>
