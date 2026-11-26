/** 时序存储「估算单 → 容量确认单 → 存储策略」一致性流程接口。 */
import { request } from './client'

export type FlowRecord = Record<string, any>

async function getJson<T = FlowRecord>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    let detail = ''
    try {
      detail = JSON.stringify(await response.json())
    } catch {
      /* ignore */
    }
    throw new Error(`接口返回 ${response.status}，数据未更新 ${detail}`)
  }
  return (await response.json()) as T
}

async function postJson<T = FlowRecord>(path: string, body: FlowRecord = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  // 冲突等业务结果也以 200 + { ok:false, conflict:true } 返回，交给页面按字段处理
  return (await response.json()) as T
}

export function listFlowPoints(): Promise<{ items: FlowRecord[] }> {
  return getJson('/api/retention/points')
}

export function getPointBasis(pointId: string): Promise<FlowRecord> {
  return getJson(`/api/retention/points/${encodeURIComponent(pointId)}/basis`)
}

export function checkCapacity(ref: string, body: FlowRecord = {}): Promise<FlowRecord> {
  return postJson(`/api/retention/estimates/${encodeURIComponent(ref)}/check`, body)
}

export function updateSampling(pointId: string, interval: string | number): Promise<FlowRecord> {
  return postJson(`/api/retention/points/${encodeURIComponent(pointId)}/sampling`, {
    sampling_interval: interval,
  })
}

export function createEstimate(body: FlowRecord): Promise<FlowRecord> {
  return postJson('/api/retention/estimates', body)
}

export function listEstimates(pointId = ''): Promise<{ items: FlowRecord[] }> {
  const query = pointId ? `?point_id=${encodeURIComponent(pointId)}` : ''
  return getJson(`/api/retention/estimates${query}`)
}

export function getEstimate(ref: string): Promise<FlowRecord> {
  return getJson(`/api/retention/estimates/${encodeURIComponent(ref)}`)
}

export function transferEstimate(ref: string): Promise<FlowRecord> {
  return postJson(`/api/retention/estimates/${encodeURIComponent(ref)}/transfer`)
}

export function confirmEstimate(ref: string, body: FlowRecord = {}): Promise<FlowRecord> {
  return postJson(`/api/retention/estimates/${encodeURIComponent(ref)}/confirm`, body)
}

export function activateEstimate(ref: string, body: FlowRecord = {}): Promise<FlowRecord> {
  return postJson(`/api/retention/estimates/${encodeURIComponent(ref)}/activate`, body)
}

export function renewEstimate(ref: string, body: FlowRecord = {}): Promise<FlowRecord> {
  return postJson(`/api/retention/estimates/${encodeURIComponent(ref)}/renew`, body)
}

export function listConfirmations(pointId = ''): Promise<{ items: FlowRecord[] }> {
  const query = pointId ? `?point_id=${encodeURIComponent(pointId)}` : ''
  return getJson(`/api/retention/confirmations${query}`)
}

export function retentionOverview(pointId = ''): Promise<FlowRecord> {
  const query = pointId ? `?point_id=${encodeURIComponent(pointId)}` : ''
  return getJson(`/api/retention/overview${query}`)
}

export function formatBytes(bytes: unknown): string {
  const value = Number(bytes ?? 0)
  if (!Number.isFinite(value)) return '—'
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(3)} GiB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(2)} MiB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${value} B`
}
