// @vitest-environment jsdom
//
// 换账号时任务模块的复位 (2026-09-27 复核 B3 的同族缺陷)。
//
// 存 localStorage 的续连键已经在 ACCOUNT_SCOPED_STORAGE_KEYS 里清了, 但**模块级单例**
// 是另一半: 登出走客户端路由不重载模块, 于是同 document 换账号后
//   ① `current` 里还留着 A 的任务 (回测/优化/滚动优化/挖掘的结果与进度) → B 页面直接渲染;
//   ② 未关闭的 EventSource 还在把 A 的任务事件收进 B 的会话;
//   ③ 挖掘的 `previousResult` 刻意跨页保留 (clearMiningTask 的语义), 但它同样是 A 的产物。
// 这里对四个模块各钉一遍: 复位后 SSE 已 close、状态已回到空、续连键已清。
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { clearAccountScopedStorage } from './account'
import { api, type MiningResult, type MiningRun } from './api'
import { tryReconnect, useBacktestTask } from './backtestTask'
import { tryReconnectOptimize, useOptimizerTask } from './optimizerTask'
import { tryReconnectWalkForward, useWalkForwardTask } from './walkforwardTask'
import { attachMiningRun, clearMiningTask, useMiningTask } from './miningTask'

vi.mock('@/lib/api', () => ({
  api: {
    miningRun: vi.fn(),
    miningResult: vi.fn(),
  },
}))

const m = vi.mocked(api)

/** 假 EventSource: 记录实例与 close 调用 (jsdom 没有 EventSource) */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly url: string
  close = vi.fn()
  onopen: (() => void) | null = null
  private readonly listeners = new Map<string, ((e: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? []
    list.push(fn)
    this.listeners.set(type, list)
  }
}

/** 回测的 connectSSE 会先 fetch 一次做预检 (拿后端 detail), 这里让它通过 */
const okProbe = {
  ok: true,
  status: 200,
  json: async () => ({}),
  body: { cancel: async () => {} },
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal('fetch', vi.fn(async () => okProbe))
  localStorage.clear()
  m.miningRun.mockReset()
  m.miningResult.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
  host.remove()
  localStorage.clear()
})

async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

/** 每个模块一个探针: 把 store 快照序列化出来, 便于断言"是不是回到空" */
function BacktestProbe() {
  return <div data-testid="state">{JSON.stringify(useBacktestTask())}</div>
}
function OptimizerProbe() {
  return <div data-testid="state">{JSON.stringify(useOptimizerTask())}</div>
}
function WalkForwardProbe() {
  return <div data-testid="state">{JSON.stringify(useWalkForwardTask())}</div>
}
function MiningProbe() {
  return <div data-testid="state">{JSON.stringify(useMiningTask())}</div>
}

async function mount(node: ReactNode) {
  await act(async () => { root.render(node) })
  await settle()
}

const state = () => JSON.parse(host.querySelector('[data-testid="state"]')!.textContent || 'null')

it('回测: 复位后 SSE 已关、任务态回空、续连键已清', async () => {
  localStorage.setItem('backtest_reconnect', 'strategy_id=s1')
  await mount(<BacktestProbe />)
  await act(async () => { expect(tryReconnect()).toBe(true) })
  await settle()

  const es = FakeEventSource.instances[0]
  expect(es, '回测应已连上 SSE').toBeDefined()
  expect(state()).not.toBeNull()

  await act(async () => { clearAccountScopedStorage() })
  await settle()

  expect(es.close).toHaveBeenCalled()          // 断掉 A 的观察通道
  expect(state()).toBeNull()                   // current 已回空
  expect(localStorage.getItem('backtest_reconnect')).toBeNull()
})

it('因子优化: 复位后 SSE 已关、任务态回空、续连键与 job key 已清', async () => {
  localStorage.setItem('optimizer_reconnect', 'q=1')
  localStorage.setItem('optimizer_job_key', 'job-A')
  await mount(<OptimizerProbe />)
  await act(async () => { expect(tryReconnectOptimize()).toBe(true) })
  await settle()

  const es = FakeEventSource.instances[0]
  expect(es).toBeDefined()
  expect(state()).not.toBeNull()

  await act(async () => { clearAccountScopedStorage() })
  await settle()

  expect(es.close).toHaveBeenCalled()
  expect(state()).toBeNull()
  expect(localStorage.getItem('optimizer_reconnect')).toBeNull()
  expect(localStorage.getItem('optimizer_job_key')).toBeNull()
})

it('滚动优化: 复位后 SSE 已关、任务态回空、续连键已清', async () => {
  localStorage.setItem('walkforward_reconnect', 'q=1')
  await mount(<WalkForwardProbe />)
  await act(async () => { expect(tryReconnectWalkForward()).toBe(true) })
  await settle()

  const es = FakeEventSource.instances[0]
  expect(es).toBeDefined()
  expect(state()).not.toBeNull()

  await act(async () => { clearAccountScopedStorage() })
  await settle()

  expect(es.close).toHaveBeenCalled()
  expect(state()).toBeNull()
  expect(localStorage.getItem('walkforward_reconnect')).toBeNull()
})

it('挖掘: 复位后 SSE 已关、连上一轮的 previousResult 也清、run id 已清', async () => {
  // A 先跑完一轮 (拿到 result), 再切页 → clearMiningTask 把它转成 previousResult 留着展示
  m.miningRun.mockResolvedValue({ run_id: 'run-A', status: 'succeeded' } as MiningRun)
  m.miningResult.mockResolvedValue({ run_id: 'run-A' } as MiningResult)
  await mount(<MiningProbe />)
  await act(async () => { await attachMiningRun('run-A') })
  await settle()
  await act(async () => { clearMiningTask() })
  expect(state().previousResult, '前置: previousResult 应已被 A 的产物填上').not.toBeNull()

  // A 又起了一轮 (未结束) → 有在跑的 SSE + 续连键
  m.miningRun.mockResolvedValue({ run_id: 'run-B', status: 'running' } as MiningRun)
  await act(async () => { await attachMiningRun('run-B') })
  await settle()
  const es = FakeEventSource.instances[0]
  expect(es, '挖掘应已连上 SSE').toBeDefined()
  expect(state().runId).toBe('run-B')

  await act(async () => { clearAccountScopedStorage() })
  await settle()

  expect(es.close).toHaveBeenCalled()
  expect(state().runId).toBeNull()
  expect(state().result).toBeNull()
  expect(state().previousResult).toBeNull()    // 它是 A 算出来的, 不能留给 B 看
  expect(localStorage.getItem('mining_active_run_id')).toBeNull()
})
