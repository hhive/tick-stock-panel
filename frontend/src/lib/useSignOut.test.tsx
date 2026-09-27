// @vitest-environment jsdom
//
// 退出登录 —— 除了清查询缓存, 还必须清掉账户私有的**指针**类本地数据, 并把身份切走
// (2026-09-27 复核 B3 + 同日口径修正)。
//
// 两件事一起钉: ① 内存单例要腾空 —— 助手 store 是模块级单例, 登出走客户端路由
// (不重载模块), 不清内存的话换人登录后抽屉里仍是上一账号的对话;
// ② 但**不许销毁**上一账号的对话本体: 助手会话按身份命名空间隔离, 登出只是切窗口,
// A 重新登录要能原样读回 (用"登出即清"的老做法, 这条会红)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { applyAccountIdentity, resetAccountStateForTests } from './account'
import { useSignOut } from './useSignOut'
import { api } from './api'

vi.mock('@/lib/api', () => ({
  api: {
    accountLogout: vi.fn(async () => ({ ok: true })),
    authLogout: vi.fn(async () => ({ ok: true })),
  },
}))

const m = vi.mocked(api)

/** A 账号留下的一段助手会话 (明文提问 + 回答), 存在 A 的命名空间键下 */
const A = 'a@example.com'
const keyFor = (identity: string) => `assistant.sessions.v1.${identity}`
const A_SESSIONS = JSON.stringify({
  sessions: [{
    id: 'a1',
    title: 'A 的对话',
    createdAt: 1,
    messages: [{ id: 'm1', role: 'user', content: 'A 问的机密问题', ts: 1 }],
  }],
  activeId: 'a1',
})

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  m.accountLogout.mockResolvedValue({ ok: true } as never)
  m.authLogout.mockResolvedValue({ ok: true } as never)
  localStorage.clear()
  resetAccountStateForTests()

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

function Harness() {
  const { signOut, signingOut } = useSignOut()
  return (
    <button data-testid="signout" disabled={signingOut} onClick={() => void signOut('account')}>
      退出
    </button>
  )
}

async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

async function render() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/" element={<Harness />} />
          <Route path="/login" element={<div data-testid="login-page">LOGIN</div>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

it('退出后内存腾空、指针类数据清掉, 但上一账号的对话本体不销毁', async () => {
  // A 在会话期间用过助手 → store 已装载 (先装载, 身份监听才登记得上), 随后身份就位
  const store = await import('@/custom/assistant/store')
  localStorage.setItem(keyFor(A), A_SESSIONS)
  localStorage.setItem('paper.account', 'acc-of-A')
  localStorage.setItem('mining_workbench_draft_v1', '{"symbols":["600000"]}')
  localStorage.setItem('tf-theme', 'dark')
  applyAccountIdentity(A)
  expect(store.activeSession()?.title).toBe('A 的对话')

  await render()
  await act(async () => { host.querySelector<HTMLButtonElement>('[data-testid="signout"]')!.click() })
  await settle()

  expect(m.accountLogout).toHaveBeenCalledTimes(1)
  // 指针类: 清
  expect(localStorage.getItem('paper.account')).toBeNull()
  expect(localStorage.getItem('mining_workbench_draft_v1')).toBeNull()
  // 内存里的那份腾空 —— 换人登录后抽屉不能还挂着上一账号的对话
  expect(store.activeSession()).toBeNull()
  // 但 A 的对话本体不许销毁: A 重新登录要能原样读回
  expect(localStorage.getItem(keyFor(A)), 'A 的对话不该被登出销毁').not.toBeNull()
  // 设备级偏好不动
  expect(localStorage.getItem('tf-theme')).toBe('dark')
  // 落到登录页
  expect(host.querySelector('[data-testid="login-page"]')).not.toBeNull()
})

it('登出接口失败 (会话已过期) 也要切身份、内存腾空并落到登录页', async () => {
  m.accountLogout.mockRejectedValue(new Error('未登录'))
  const store = await import('@/custom/assistant/store')
  localStorage.setItem(keyFor(A), A_SESSIONS)
  applyAccountIdentity(A)
  expect(store.activeSession()?.title).toBe('A 的对话')

  await render()
  await act(async () => { host.querySelector<HTMLButtonElement>('[data-testid="signout"]')!.click() })
  await settle()

  expect(store.activeSession()).toBeNull()
  expect(localStorage.getItem(keyFor(A))).not.toBeNull()
  expect(host.querySelector('[data-testid="login-page"]')).not.toBeNull()
})
