// @vitest-environment jsdom
//
// AI 设置页的上游锁定 —— 面板 AI 只能走本站 Sub2API 网关(用户 2026-09-26 决策)。
// 这里只钉三件事: 地址只读固定、保存时提交的就是这个地址、其它预设不再出现在页面上。
// 服务端强制(请求里传别的地址也改不了)由后端 tests/test_ai_gateway_lock.py 负责。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type SettingsState } from '@/lib/api'
import { SettingsAIPanel } from './AI'

const LOCKED_BASE_URL = 'https://xiaoni-model.top/v1'

const h = vi.hoisted(() => ({ state: {} as SettingsState }))

vi.mock('@/lib/useSharedQueries', () => ({
  useSettings: () => ({ data: h.state }),
}))

vi.mock('@/lib/api', () => ({
  api: {
    saveAiSettings: vi.fn(),
    clearAiSettings: vi.fn(),
    strategyAiTest: vi.fn(),
    aiModels: vi.fn(),
  },
}))

const m = vi.mocked(api)

function baseState(over: Partial<SettingsState> = {}): SettingsState {
  return {
    mode: 'none',
    tickflow_api_key_masked: '',
    has_tickflow_key: false,
    tier_label: '',
    current_endpoint: '',
    probe_log: [],
    missing_caps: [],
    extras_caps: [],
    onboarding_completed: true,
    ai_provider: 'openai_compat',
    ai_base_url: LOCKED_BASE_URL,
    ai_api_key_masked: '',
    has_ai_key: false,
    ai_model: '',
    ai_user_agent: '',
    ...over,
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  h.state = baseState()
  m.saveAiSettings.mockResolvedValue({ ok: true, ai_provider: 'openai_compat', ai_base_url: LOCKED_BASE_URL } as never)

  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPanel() {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <SettingsAIPanel />
    </QueryClientProvider>,
  ))
  await settle()
}

const addrInput = () => host.querySelector<HTMLInputElement>('input[aria-label="AI 网关地址"]')
const modelInput = () => host.querySelector<HTMLInputElement>('input[placeholder="gpt-5.6-sol"]')
const buttonByText = (text: string) =>
  Array.from(host.querySelectorAll('button')).find(b => (b.textContent ?? '').includes(text))

function typeInto(input: HTMLInputElement | null, value: string) {
  if (!input) throw new Error('input not found')
  // 必须走原型上的原生 setter: React 在节点上留了 value tracker, 直接赋值再派发
  // input 事件会被它判定为「值没变」而丢弃, 受控组件因而收不到这次输入。
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

it('API 地址只读且写死为本站网关', async () => {
  await renderPanel()

  const addr = addrInput()
  expect(addr).toBeTruthy()
  expect(addr!.value).toBe(LOCKED_BASE_URL)
  expect(addr!.readOnly).toBe(true)
})

it('服务端返回的地址优先于本地常量，界面不谎报请求实际打向哪里', async () => {
  h.state = baseState({ ai_base_url: 'https://elsewhere.example/v1' })
  await renderPanel()

  expect(addrInput()!.value).toBe('https://elsewhere.example/v1')
})

it('服务端尚未返回地址时用本地常量兜底，不显示空值', async () => {
  h.state = baseState({ ai_base_url: '' })
  await renderPanel()

  expect(addrInput()!.value).toBe(LOCKED_BASE_URL)
})

it('保存时提交锁定地址与自定义 provider，用户改不了上游', async () => {
  await renderPanel()

  // 只有模型和 Key 是需要用户填的; 地址栏没有可编辑的余地
  await act(async () => { typeInto(modelInput(), 'gpt-x') })
  await settle()

  await act(async () => { buttonByText('保存配置')?.click() })
  await settle()

  expect(m.saveAiSettings).toHaveBeenCalledTimes(1)
  const payload = m.saveAiSettings.mock.calls[0][0] as Record<string, unknown>
  expect(payload.base_url).toBe(LOCKED_BASE_URL)
  expect(payload.provider).toBe('openai_compat')
})

it('页面上不再有其它 AI 上游预设', async () => {
  await renderPanel()

  const text = host.textContent ?? ''
  for (const gone of ['快速预设', 'RunningHub', 'Codex', 'DeepSeek', '智谱', 'Kimi', '通义']) {
    expect(text).not.toContain(gone)
  }
})
