// @vitest-environment jsdom
//
// 「我自己的 Key」区块 —— 用户填了自己的数据源 Key 就以他的为准, 清掉回落站点共用。
// 这里钉: 来源显示正确、保存打对端点、有自己 Key 时才能「改回站点」。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type PluginDataSourceItem } from '@/lib/api'
import { MySourceKeyConfig } from './DataSources'

vi.mock('@/lib/api', () => ({
  api: {
    saveUserSourceKey: vi.fn(),
    clearUserSourceKey: vi.fn(),
  },
}))

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const m = vi.mocked(api)

const plugin = (over: Partial<PluginDataSourceItem> = {}): PluginDataSourceItem => ({
  name: 'fuyao',
  display_name: '扶摇',
  datasets: ['daily'],
  runtime: 'python',
  available: true,
  status: 'ok',
  description: '',
  install_hint: '',
  api_key_env: 'FUYAO_API_KEY',
  api_key_masked: 'sk-s......ites',
  ...over,
})

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  m.saveUserSourceKey.mockResolvedValue({ ok: true, scope: 'user' } as never)
  m.clearUserSourceKey.mockResolvedValue({ ok: true, scope: 'user' } as never)

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
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

async function render(p: PluginDataSourceItem) {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <MySourceKeyConfig plugin={p} />
    </QueryClientProvider>,
  ))
  await settle()
}

const keyInput = () => host.querySelector<HTMLInputElement>('input[type="password"]')
/** 只看「当前使用」那一行 —— 说明文案里也有「你自己的」, 全文断言会误判 */
const inUse = () => host.querySelector('[data-testid="source-key-in-use"]')?.textContent ?? ''
const buttonByText = (t: string) =>
  Array.from(host.querySelectorAll('button')).find(b => (b.textContent ?? '').includes(t))

function typeInto(input: HTMLInputElement | null, value: string) {
  if (!input) throw new Error('input not found')
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

it('没填过自己的 Key 时显示「站点共用的」，且不给「改回站点」', async () => {
  await render(plugin())

  expect(inUse()).toContain('站点共用的')
  expect(inUse()).not.toContain('你自己的')
  expect(buttonByText('改回站点')).toBeFalsy()
})

it('填过自己的 Key 时显示来源为「你自己的」并给出改回入口', async () => {
  await render(plugin({ user_api_key_masked: 'sk-m......mine' }))

  expect(inUse()).toContain('你自己的')
  expect(inUse()).toContain('sk-m......mine')
  expect(buttonByText('改回站点')).toBeTruthy()
})

it('保存打的是「我自己的」端点，不是站点那把', async () => {
  await render(plugin())

  await act(async () => { typeInto(keyInput(), 'sk-mine') })
  await settle()
  await act(async () => { buttonByText('保存并使用')?.click() })
  await settle()

  expect(m.saveUserSourceKey).toHaveBeenCalledWith('fuyao', 'sk-mine')
})

it('改回站点调用清除端点', async () => {
  await render(plugin({ user_api_key_masked: 'sk-m......mine' }))

  await act(async () => { buttonByText('改回站点')?.click() })
  await settle()

  expect(m.clearUserSourceKey).toHaveBeenCalledWith('fuyao')
})
