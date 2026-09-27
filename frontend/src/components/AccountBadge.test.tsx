// @vitest-environment jsdom
//
// 侧边栏账号入口 —— 用户报告「页面上看不到注册登录退出账号的地方」。
// 这里钉四件事: 未登录给登录入口、已登录显示身份、退出按当前会话类型调对端点、
// 状态未知时不闪「未登录」。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type AuthStatus } from '@/lib/api'
import { AccountBadge } from './AccountBadge'

const h = vi.hoisted(() => ({ status: undefined as AuthStatus | undefined }))

vi.mock('@/lib/api', () => ({
  api: {
    authStatus: vi.fn(async () => h.status),
    authLogout: vi.fn(async () => ({ ok: true })),
    accountLogout: vi.fn(async () => ({ ok: true })),
  },
}))

const m = vi.mocked(api)

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  m.authStatus.mockImplementation(async () => h.status as AuthStatus)
  m.authLogout.mockResolvedValue({ ok: true } as never)
  m.accountLogout.mockResolvedValue({ ok: true } as never)

  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  localStorage.clear()
})

const status = (over: Partial<AuthStatus>): AuthStatus => ({
  configured: true, authenticated: true, claimed: true,
  mode: 'account', role: 'user', email: 'owner@example.com',
  ...over,
})

async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

async function render() {
  await act(async () => root.render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AccountBadge />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

const button = () => host.querySelector<HTMLButtonElement>('button')
const menuItem = (text: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    .find(b => (b.textContent ?? '').includes(text))

it('未登录时给出登录入口，且不显示退出', async () => {
  h.status = status({ authenticated: false, mode: 'guest', role: 'guest', email: null })
  await render()

  expect(host.textContent).toContain('未登录')
  expect(host.textContent).toContain('登录')
  expect(host.textContent).not.toContain('退出')
})

it('已登录时显示当前账号，收起状态下不显示角色（宽度留给邮箱）', async () => {
  h.status = status({ role: 'admin' })
  await render()

  expect(host.textContent).toContain('owner@example.com')
  expect(host.textContent).not.toContain('未登录')
  expect(host.textContent).not.toContain('管理员')
})

it('角色与完整身份放在菜单里', async () => {
  h.status = status({ role: 'admin' })
  await render()
  await act(async () => { button()?.click() })
  await settle()

  expect(host.textContent).toContain('管理员')
})

it('普通用户在菜单里显示为普通用户', async () => {
  h.status = status({ role: 'user' })
  await render()
  await act(async () => { button()?.click() })
  await settle()

  expect(host.textContent).toContain('普通用户')
  expect(host.textContent).not.toContain('管理员')
})

it('退出菜单默认收起，点开后出现「退出登录」', async () => {
  await render()
  expect(menuItem('退出登录')).toBeFalsy()

  await act(async () => { button()?.click() })
  await settle()

  expect(menuItem('退出登录')).toBeTruthy()
})

it('从这里退出也会清掉账户私有的指针数据 (同浏览器换人用)', async () => {
  localStorage.setItem('assistant.sessions.v1.prev@example.com', '{"sessions":[],"activeId":""}')
  localStorage.setItem('paper.account', 'acc-of-previous')
  localStorage.setItem('tf-theme', 'dark')

  await render()
  await act(async () => { button()?.click() })
  await settle()
  await act(async () => { menuItem('退出登录')?.click() })
  await settle()

  expect(localStorage.getItem('paper.account')).toBeNull()
  expect(localStorage.getItem('tf-theme')).toBe('dark')
  // 对话本体是别人的用户内容, 不销毁 (按身份命名空间隔离, 见 assistantIdentityScope.test.ts)
  expect(localStorage.getItem('assistant.sessions.v1.prev@example.com')).not.toBeNull()
})

it('账号会话退出走 accountLogout，不走单密码端点', async () => {
  await render()
  await act(async () => { button()?.click() })
  await settle()
  await act(async () => { menuItem('退出登录')?.click() })
  await settle()

  expect(m.accountLogout).toHaveBeenCalledTimes(1)
  expect(m.authLogout).not.toHaveBeenCalled()
})

it('单密码应急会话退出走 authLogout —— 两套会话不能混', async () => {
  h.status = status({ mode: 'legacy', email: null })
  await render()
  await act(async () => { button()?.click() })
  await settle()
  await act(async () => { menuItem('退出登录')?.click() })
  await settle()

  expect(m.authLogout).toHaveBeenCalledTimes(1)
  expect(m.accountLogout).not.toHaveBeenCalled()
})

it('状态未知时什么都不渲染，不闪一下「未登录」', async () => {
  // 请求挂起(而不是返回 undefined)才是真实的「还不知道」状态
  m.authStatus.mockImplementation(() => new Promise(() => {}) as never)
  await render()

  expect(host.textContent ?? '').toBe('')
})
