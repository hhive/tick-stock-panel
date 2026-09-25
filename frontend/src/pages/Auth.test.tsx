// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { SUB2API_SITE_URL, resetAccountStateForTests } from '@/lib/account'
import { JumpGate, resetJumpStateForTests } from '@/components/JumpGate'
import { Auth } from './Auth'

// 网络层整体 mock —— 本文件绝不发真实请求。
// ApiError 也要一起 mock: JumpGate 用 instanceof 区分「凭证失效(401)」与「网络/服务端异常」。
const mock = vi.hoisted(() => {
  class MockApiError extends Error {
    readonly status: number
    constructor(message: string, status: number) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return { MockApiError }
})

vi.mock('@/lib/api', () => ({
  ApiError: mock.MockApiError,
  api: {
    accountJump: vi.fn(),
    accountRegister: vi.fn(),
    accountLogin: vi.fn(),
    accountLogout: vi.fn(),
    accountMe: vi.fn(),
    accountBind: vi.fn(),
    accountUnbind: vi.fn(),
    authStatus: vi.fn(),
    authSetup: vi.fn(),
    authLogin: vi.fn(),
  },
}))

const m = vi.mocked(api)
const JUMP_KEY = 'sk-jump-test'

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  resetAccountStateForTests()
  resetJumpStateForTests()

  // 默认: 面板已设过密码, 当前未登录
  m.authStatus.mockResolvedValue({ configured: true, authenticated: false })
  m.accountBind.mockResolvedValue({ ok: true })

  // jsdom 的地址栏独立于 router 的 location, 跳转凭证的来源以地址栏为准
  window.history.replaceState(null, '', `/dashboard?apikey=${JUMP_KEY}`)

  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    mutations: { retry: false },
  } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  window.history.replaceState(null, '', '/')
})

// React Query / router 的重定向都排在后续的微任务与定时器里, 多刷几轮。
async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

/** 走完整启动链路: JumpGate(处理 ?apikey=) → 页面路由 */
async function renderApp(path = `/dashboard?apikey=${JUMP_KEY}`) {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route element={<JumpGate />}>
            <Route path="/" element={<div data-testid="app-home">HOME</div>} />
            <Route path="/dashboard" element={<div data-testid="app-dashboard">DASHBOARD</div>} />
            <Route path="/login" element={<Auth />} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

/** 只渲染登录页(不经跳转门) */
async function renderAuthOnly() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/login']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/" element={<div data-testid="app-home">HOME</div>} />
          <Route path="/login" element={<Auth />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

const nativeValueSetter =
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!

/** 受控输入: 必须绕过 React 的 value tracker 直接写 DOM 再派发 input 事件 */
function typeInto(selector: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(selector)
  expect(input, `找不到输入框 ${selector}`).not.toBeNull()
  act(() => {
    nativeValueSetter!.call(input, value)
    input!.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function click(selector: string) {
  const el = host.querySelector<HTMLElement>(selector)
  expect(el, `找不到元素 ${selector}`).not.toBeNull()
  await act(async () => el!.click())
  await settle()
}

function text() {
  return host.textContent || ''
}

// ===== 跳转门 =====

it('logged_in: 用跳转凭证换取会话后直接进入面板', async () => {
  m.accountJump.mockResolvedValue({ status: 'logged_in', email: 'vip@example.com' })

  await renderApp()

  expect(m.accountJump).toHaveBeenCalledWith(JUMP_KEY)
  expect(host.querySelector('[data-testid="app-dashboard"]')).not.toBeNull()
  expect(host.querySelector('[data-testid="email-input"]')).toBeNull()
})

it('needs_auth: 转到登录页并在内存里留住凭证', async () => {
  m.accountJump.mockResolvedValue({ status: 'needs_auth' })

  await renderApp()

  // 登录页(邮箱密码表单)已渲染, 且提示了待绑定的凭证
  expect(host.querySelector('[data-testid="email-input"]')).not.toBeNull()
  expect(host.querySelector('[data-testid="mode-register"]')).not.toBeNull()
  expect(host.querySelector('[data-testid="jump-held-hint"]')).not.toBeNull()
  // 凭证已从地址栏消失, 没有落进路由的可分享状态
  expect(window.location.href).not.toContain(JUMP_KEY)
})

it('401: 提示 API Key 无效或已失效', async () => {
  m.accountJump.mockRejectedValue(new mock.MockApiError('API Key 无效', 401))

  await renderApp()

  expect(text()).toContain('API Key 无效或已失效')
  expect(host.querySelector('[data-testid="app-dashboard"]')).toBeNull()
  expect(host.querySelector('[data-testid="email-input"]')).toBeNull()
})

it('网络异常: 显示可重试的错误, 重试成功后进入登录页', async () => {
  m.accountJump.mockRejectedValue(new TypeError('Failed to fetch'))

  await renderApp()

  expect(text()).toContain('网络异常')
  expect(host.querySelector('[data-testid="jump-retry"]')).not.toBeNull()

  // 重试时地址栏里已无凭证, 必须靠内存里留住的那份
  m.accountJump.mockResolvedValue({ status: 'needs_auth' })
  await click('[data-testid="jump-retry"]')

  expect(m.accountJump).toHaveBeenLastCalledWith(JUMP_KEY)
  expect(host.querySelector('[data-testid="email-input"]')).not.toBeNull()
})

it('启动即抹掉地址栏里的凭证 (history.replaceState)', async () => {
  const replaceState = vi.spyOn(window.history, 'replaceState')
  m.accountJump.mockResolvedValue({ status: 'logged_in', email: 'vip@example.com' })

  await renderApp()

  expect(replaceState).toHaveBeenCalled()
  expect(window.location.search).toBe('')
  expect(window.location.pathname).toBe('/dashboard')
  expect(window.location.href).not.toContain(JUMP_KEY)
})

// ===== 登录页 =====

it('主操作渲染为「从 Sub2API 一键进入」', async () => {
  await renderAuthOnly()

  const link = host.querySelector<HTMLAnchorElement>('[data-testid="sub2api-enter"]')
  expect(link).not.toBeNull()
  expect(link!.getAttribute('href')).toBe(SUB2API_SITE_URL)
  expect(link!.getAttribute('href')).toMatch(/^https:\/\//)
  expect(link!.textContent).toContain('从 Sub2API 一键进入')
  // 邮箱密码表单是次选, 但必须在同一屏可达
  expect(host.querySelector('[data-testid="email-input"]')).not.toBeNull()
})

it('注册成功后绑定持有的跳转凭证并进入面板', async () => {
  m.accountJump.mockResolvedValue({ status: 'needs_auth' })
  m.accountRegister.mockResolvedValue({ ok: true, email: 'new@example.com' })

  await renderApp()
  // 带跳转凭证进来时默认落在注册页签
  typeInto('[data-testid="email-input"]', 'new@example.com')
  typeInto('[data-testid="password-input"]', 'secret123')
  typeInto('[data-testid="password2-input"]', 'secret123')
  await click('[data-testid="email-submit"]')

  expect(m.accountRegister).toHaveBeenCalledWith('new@example.com', 'secret123')
  expect(m.accountBind).toHaveBeenCalledWith(JUMP_KEY)
  expect(host.querySelector('[data-testid="app-home"]')).not.toBeNull()
})

it('登录成功后绑定持有的跳转凭证并进入面板', async () => {
  m.accountJump.mockResolvedValue({ status: 'needs_auth' })
  m.accountLogin.mockResolvedValue({ ok: true, email: 'vip@example.com' })

  await renderApp()
  await click('[data-testid="mode-login"]')
  typeInto('[data-testid="email-input"]', 'vip@example.com')
  typeInto('[data-testid="password-input"]', 'secret123')
  await click('[data-testid="email-submit"]')

  expect(m.accountLogin).toHaveBeenCalledWith('vip@example.com', 'secret123')
  expect(m.accountBind).toHaveBeenCalledWith(JUMP_KEY)
  expect(host.querySelector('[data-testid="app-home"]')).not.toBeNull()
})

it('没有跳转凭证时登录成功不会去绑定', async () => {
  m.accountLogin.mockResolvedValue({ ok: true, email: 'vip@example.com' })

  await renderAuthOnly()
  typeInto('[data-testid="email-input"]', 'vip@example.com')
  typeInto('[data-testid="password-input"]', 'secret123')
  await click('[data-testid="email-submit"]')

  expect(m.accountLogin).toHaveBeenCalledTimes(1)
  expect(m.accountBind).not.toHaveBeenCalled()
})

it('密码不足 6 位时本地拦截, 不发请求', async () => {
  await renderAuthOnly()
  await click('[data-testid="mode-register"]')
  typeInto('[data-testid="email-input"]', 'new@example.com')
  typeInto('[data-testid="password-input"]', '12345')
  typeInto('[data-testid="password2-input"]', '12345')
  await click('[data-testid="email-submit"]')

  expect(text()).toContain('密码至少 6 位')
  expect(m.accountRegister).not.toHaveBeenCalled()
})

it('两次密码不一致时本地拦截, 不发请求', async () => {
  await renderAuthOnly()
  await click('[data-testid="mode-register"]')
  typeInto('[data-testid="email-input"]', 'new@example.com')
  typeInto('[data-testid="password-input"]', 'secret123')
  typeInto('[data-testid="password2-input"]', 'secret124')
  await click('[data-testid="email-submit"]')

  expect(text()).toContain('两次密码不一致')
  expect(m.accountRegister).not.toHaveBeenCalled()
})

it('邮箱格式不合法时本地拦截, 不发请求', async () => {
  await renderAuthOnly()
  typeInto('[data-testid="email-input"]', 'not-an-email')
  typeInto('[data-testid="password-input"]', 'secret123')
  await click('[data-testid="email-submit"]')

  expect(text()).toContain('请输入正确的邮箱地址')
  expect(m.accountLogin).not.toHaveBeenCalled()
})

it('429 限流显示中文可读提示 (后端未给 detail 时也不露出英文兜底文案)', async () => {
  m.accountLogin.mockRejectedValue(new mock.MockApiError('429 Too Many Requests', 429))

  await renderAuthOnly()
  typeInto('[data-testid="email-input"]', 'vip@example.com')
  typeInto('[data-testid="password-input"]', 'secret123')
  await click('[data-testid="email-submit"]')

  expect(host.querySelector('[data-testid="auth-error"]')).not.toBeNull()
  expect(text()).toContain('操作过于频繁, 请稍后再试')
  expect(text()).not.toContain('Too Many Requests')
})

it('401 登录失败显示邮箱或密码错误', async () => {
  m.accountLogin.mockRejectedValue(new mock.MockApiError('邮箱或密码错误', 401))

  await renderAuthOnly()
  typeInto('[data-testid="email-input"]', 'vip@example.com')
  typeInto('[data-testid="password-input"]', 'wrongpass')
  await click('[data-testid="email-submit"]')

  expect(text()).toContain('邮箱或密码错误')
  expect(host.querySelector('[data-testid="app-home"]')).toBeNull()
})

it('旧访问密码通道仍可用 (不回归单密码流程)', async () => {
  m.authLogin.mockResolvedValue({ ok: true })

  await renderAuthOnly()
  await click('[data-testid="toggle-legacy"]')
  typeInto('input[placeholder="访问密码"]', 'secret123')
  await click('[data-testid="legacy-submit"]')

  expect(m.authLogin).toHaveBeenCalledWith('secret123')
  expect(host.querySelector('[data-testid="app-home"]')).not.toBeNull()
})
