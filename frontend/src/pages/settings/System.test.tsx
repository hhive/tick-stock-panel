// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { SettingsSystemPanel } from './System'

vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: undefined }),
  useVersion: () => ({ data: undefined }),
}))

vi.mock('@/lib/api', () => ({
  api: {
    authStatus: vi.fn(),
    accountLogout: vi.fn(),
    authLogout: vi.fn(),
    updateRealtimeMonitorConfig: vi.fn(),
  },
}))

const m = vi.mocked(api)

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  m.accountLogout.mockResolvedValue({ ok: true })
  m.authLogout.mockResolvedValue({ ok: true })

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
  // 预置一份缓存: 退出登录必须把它清干净, 否则换账号会看到上一个账号的数据
  client.setQueryData(['watchlist'], ['600000.SH'])
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/settings?tab=system']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/settings" element={<SettingsSystemPanel />} />
          <Route path="/login" element={<div data-testid="login-page">LOGIN</div>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

async function clickLogout() {
  const button = host.querySelector<HTMLButtonElement>('[data-testid="logout"]')
  expect(button, '账号区应渲染退出登录按钮').not.toBeNull()
  await act(async () => button!.click())
  await settle()
}

it('账号会话: 退出登录调用 /api/account/logout, 清空缓存并落到登录页', async () => {
  m.authStatus.mockResolvedValue({
    configured: true, authenticated: true, claimed: true,
    mode: 'account', role: 'user', email: 'vip@example.com',
  })

  await renderPanel()

  // 先能看出当前是谁登录
  expect(host.textContent).toContain('vip@example.com')

  await clickLogout()

  expect(m.accountLogout).toHaveBeenCalledTimes(1)
  expect(m.authLogout).not.toHaveBeenCalled()
  expect(client.getQueryCache().getAll()).toHaveLength(0)
  expect(host.querySelector('[data-testid="login-page"]')).not.toBeNull()
  expect(host.textContent).not.toContain('vip@example.com')
})

it('单密码应急会话: 退出登录走 /api/auth/logout', async () => {
  m.authStatus.mockResolvedValue({
    configured: true, authenticated: true, claimed: true,
    mode: 'legacy', role: 'admin', email: null,
  })

  await renderPanel()
  expect(host.textContent).toContain('访问密码登录')

  await clickLogout()

  expect(m.authLogout).toHaveBeenCalledTimes(1)
  expect(m.accountLogout).not.toHaveBeenCalled()
  expect(host.querySelector('[data-testid="login-page"]')).not.toBeNull()
})

it('登出接口失败也要落到未登录态, 不把用户卡在已登录界面', async () => {
  m.authStatus.mockResolvedValue({
    configured: true, authenticated: true, claimed: true,
    mode: 'account', role: 'user', email: 'vip@example.com',
  })
  m.accountLogout.mockRejectedValue(new Error('network down'))

  await renderPanel()
  await clickLogout()

  expect(client.getQueryCache().getAll()).toHaveLength(0)
  expect(host.querySelector('[data-testid="login-page"]')).not.toBeNull()
})
