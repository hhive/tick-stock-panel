// @vitest-environment jsdom
//
// 助手会话按身份命名空间 —— 2026-09-27 口径修正 (原「登出即清」改为「按身份隔离」)。
//
// 原做法为了保护 B 不看到 A 的对话, 把 A 自己的对话也一并销毁了 —— 而
// `assistant.sessions.v1` 是这些对话**唯一**的存放处 (后端无状态)。改成
// `assistant.sessions.v1.<身份>` 后:
//   ① A 登出 → B 登录 → B 读到自己那份 (空), 看不到 A 的;
//   ② B 用一阵子再登出 → A 重新登录 → **A 的对话原样回来** (这条是本次改口径的核心);
//   ③ 切换身份只换命名空间, 不删任何人的数据。
import { beforeEach, expect, it } from 'vitest'
import { applyAccountIdentity, normalizeIdentity, resetAccountStateForTests } from './account'
import { activeSession, newSession, selectSession } from '@/custom/assistant/store'

const A = 'a@example.com'
const B = 'b@example.com'
const keyFor = (identity: string) => `assistant.sessions.v1.${identity}`

/** A 早先留下的一段对话 (含明文提问) */
const A_SESSIONS = JSON.stringify({
  sessions: [{
    id: 'a1',
    title: 'A 的对话',
    createdAt: 1,
    messages: [{ id: 'm1', role: 'user', content: 'A 问的机密问题', ts: 1 }],
  }],
  activeId: 'a1',
})

beforeEach(() => {
  localStorage.clear()
  resetAccountStateForTests()
})

it('A 写入 → 登出 → B 登录看到空, 且 A 的那份仍在盘上', () => {
  // A 登录并真的写下一条会话 (走 store 的落盘路径, 不是直接塞 localStorage)
  applyAccountIdentity(A)
  newSession()
  const aKey = keyFor(A)
  expect(JSON.parse(localStorage.getItem(aKey)!).sessions).toHaveLength(1)

  // A 登出: 内存腾空, 但盘上那份不许动
  applyAccountIdentity(null)
  expect(activeSession()).toBeNull()
  expect(JSON.parse(localStorage.getItem(aKey)!).sessions, 'A 的对话不该被销毁').toHaveLength(1)

  // B 登录: 读到自己那份 (空), 看不到 A 的
  applyAccountIdentity(B)
  expect(activeSession()).toBeNull()
  expect(localStorage.getItem(keyFor(B)), 'B 还没写过, 不该凭空有数据').toBeNull()
})

it('B 写入后登出, A 重新登录原样读回自己的对话', () => {
  localStorage.setItem(keyFor(A), A_SESSIONS)

  // A 登录 → 读回自己的对话
  applyAccountIdentity(A)
  expect(activeSession()?.title).toBe('A 的对话')

  // A 登出、B 登录并写下自己的会话
  applyAccountIdentity(null)
  applyAccountIdentity(B)
  newSession()
  expect(activeSession()?.title).toBe('新对话')

  // B 登出、A 重新登录 → A 的对话原样回来 (不是"被清掉了")
  applyAccountIdentity(null)
  applyAccountIdentity(A)
  expect(activeSession()?.title).toBe('A 的对话')
  expect(activeSession()?.messages[0]).toMatchObject({ role: 'user', content: 'A 问的机密问题' })
  // B 的那份也还在 (切身份不删任何人的数据)
  expect(localStorage.getItem(keyFor(B))).not.toBeNull()
})

it('身份归一化: 大小写与首尾空白算同一个人, 不触发重复切换', () => {
  expect(normalizeIdentity('  A@Example.COM ')).toBe(A)
  expect(normalizeIdentity('')).toBe(normalizeIdentity(null))
  expect(normalizeIdentity(null)).toBe(normalizeIdentity(undefined))

  applyAccountIdentity(A)
  newSession()
  // 造一个「只在内存、不落盘」的状态: selectSession 不持久化 (activeId 指向不存在的会话),
  // 一旦发生重新装载就会被盘上的 activeId 覆盖回来 —— 用它分辨"有没有重新装载"。
  selectSession('not-on-disk')
  expect(activeSession()).toBeNull()

  // 同一个人的另一种写法: 不该被当成换人
  applyAccountIdentity('A@Example.com')
  expect(activeSession(), '同一身份的不同写法不该触发重新装载').toBeNull()
})

it('登出不销毁任何助手会话键 (只清指针类账户私有键)', () => {
  localStorage.setItem(keyFor(A), A_SESSIONS)
  localStorage.setItem(keyFor(B), A_SESSIONS)

  applyAccountIdentity(null)

  expect(localStorage.getItem(keyFor(A))).not.toBeNull()
  expect(localStorage.getItem(keyFor(B))).not.toBeNull()
})
