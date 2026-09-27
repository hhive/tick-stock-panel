// @vitest-environment jsdom
//
// 跨账号本地残留清理 —— 2026-09-27 复核 B3。
//
// 这里钉三件事, 每一件都是实证过的缺陷:
//   ① 账户私有键 (助手会话历史 / 模拟盘账户 / 挖掘草稿) 调用后必被清 —— 否则同浏览器
//      上 A 退出、B 登录后, B 能直接读到 A 的 AI 对话全文;
//   ② 设备级偏好 (主题 / 列宽 / 提示音) **不能**被顺手抹掉 —— 那些跨账号保留是合理的;
//   ③ 登记在清单里的复位回调要被执行, 且一个回调抛错不影响其它回调。
import { beforeEach, expect, it, vi } from 'vitest'
import {
  ACCOUNT_SCOPED_STORAGE_KEYS,
  clearAccountScopedStorage,
  registerAccountScopedReset,
} from './account'

/**
 * 必须被清的**指针/断点**类键 —— 用字面量写死, 清单里漏登记任何一个这条就红。
 * (助手会话**不在**此列: 它是用户内容, 2026-09-27 口径修正后按身份命名空间隔离,
 * 登出只切命名空间、不销毁 —— 见 assistantIdentityScope.test.ts)
 */
const MUST_CLEAR_KEYS = [
  'paper.account',
  'mining_workbench_draft_v1',
]

/** 设备级偏好 (跨账号保留): 主题 / 导航态 / 列宽 / 告警提示音等 */
const DEVICE_PREFS: Record<string, string> = {
  'tf-theme': 'dark',
  'tf-nav-state': 'rail',
  'tf-settings-nav-collapsed': '1',
  'alert_toast_enabled': '0',
  'alert_sound_enabled': '1',
  'alert_sound': 'ding',
  'voice_broadcast_voice': 'zh-CN',
  'monitor_badge_enabled': '0',
  'assistant.width.v1': '420',
  'assistant.fab.pos.v1': '{"x":1,"y":2}',
  'ai_bubble_pos': '{"x":3,"y":4}',
  'factors-intro-collapsed': '1',
  'tsp_adj_factor_no_remind': '1',
}

beforeEach(() => {
  localStorage.clear()
})

it('清掉账户私有的指针类键: 模拟盘账户 / 挖掘草稿', () => {
  for (const key of MUST_CLEAR_KEYS) localStorage.setItem(key, 'A 的内容')

  clearAccountScopedStorage()

  for (const key of MUST_CLEAR_KEYS) {
    expect(localStorage.getItem(key), `${key} 应被清掉`).toBeNull()
  }
})

it('助手会话(用户内容)不被清: 按身份命名空间隔离, 不销毁', () => {
  localStorage.setItem('assistant.sessions.v1.a@example.com', 'A 的对话')
  localStorage.setItem('assistant.sessions.v1', '旧的无命名空间键(惰性)')

  clearAccountScopedStorage()

  expect(localStorage.getItem('assistant.sessions.v1.a@example.com')).not.toBeNull()
  expect(localStorage.getItem('assistant.sessions.v1')).not.toBeNull()
})

it('清单里的每一个键都被清掉, 且清完后不留任何键', () => {
  expect(ACCOUNT_SCOPED_STORAGE_KEYS.length).toBeGreaterThan(0)
  for (const key of ACCOUNT_SCOPED_STORAGE_KEYS) localStorage.setItem(key, 'A 的内容')

  clearAccountScopedStorage()

  expect(localStorage.length).toBe(0)
})

it('设备级偏好不被清 —— 主题/列宽/提示音跨账号保留', () => {
  for (const [key, value] of Object.entries(DEVICE_PREFS)) localStorage.setItem(key, value)

  clearAccountScopedStorage()

  for (const [key, value] of Object.entries(DEVICE_PREFS)) {
    expect(localStorage.getItem(key), `${key} 不该被清`).toBe(value)
  }
})

it('账户私有键与设备级偏好混在一起时, 只清账户私有键', () => {
  localStorage.setItem('paper.account', 'acc-of-A')
  localStorage.setItem('mining_workbench_draft_v1', '{"symbols":["600000"]}')
  localStorage.setItem('tf-theme', 'dark')

  clearAccountScopedStorage()

  expect(localStorage.getItem('paper.account')).toBeNull()
  expect(localStorage.getItem('mining_workbench_draft_v1')).toBeNull()
  expect(localStorage.getItem('tf-theme')).toBe('dark')
})

it('已登记的内存复位回调被执行 (清理不只看 localStorage)', () => {
  const reset = vi.fn()
  registerAccountScopedReset(reset)

  clearAccountScopedStorage()

  expect(reset).toHaveBeenCalledTimes(1)
})

it('单个复位回调抛错不影响其它回调与清理本身', () => {
  const boom = vi.fn(() => { throw new Error('boom') })
  const ok = vi.fn()
  registerAccountScopedReset(boom)
  registerAccountScopedReset(ok)
  localStorage.setItem('paper.account', 'acc-of-A')
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

  expect(() => clearAccountScopedStorage()).not.toThrow()
  expect(ok).toHaveBeenCalledTimes(1)
  expect(localStorage.getItem('paper.account')).toBeNull()
  warn.mockRestore()
})
