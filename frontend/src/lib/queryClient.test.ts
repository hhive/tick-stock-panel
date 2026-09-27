// @vitest-environment jsdom
//
// 全局 query 客户端的失败重试策略 —— D2「游客不再抖动、不再白耗限流」(2026-09-27)。
//
// 为什么钉「请求次数」而不是钉配置对象: 配置漏写时看形状看不出来, 真实后果是每个
// 失败请求被放大成 4 次 (React Query 默认 retry=3, 退避 1s/2s/4s) —— 游客被弹到
// 登录页前要白等约 7 秒, 期间十余个受保护端点各多打 3 个必然 401 的请求。
//
// 用 QueryObserver 而不是 client.fetchQuery(): 后者在 query-core 里会强制
// `retry = false`(见 query-core/queryClient.js 的 fetchQuery), 于是「401 只发一次」
// 这类断言在**没有**任何重试配置时也会绿 —— 一条假的绿。QueryObserver 才是 useQuery
// 走的路径(undefined 时由 retryer 兜到 3 次), 这里必须用真实路径数请求次数。
import { expect, it, vi } from 'vitest'
import { QueryObserver } from '@tanstack/react-query'
import { ApiError } from './api'
import { createQueryClient } from './queryClient'

/** 用真实客户端跑一次失败查询, 返回 queryFn 实际被调用的次数 */
async function attemptsFor(error: unknown): Promise<number> {
  const client = createQueryClient(() => {})
  const queryFn = vi.fn(async () => { throw error })
  const observer = new QueryObserver(client, {
    queryKey: ['probe'],
    queryFn,
    // 只是让重试在测试里不必等 1s/2s/4s; retry 本身仍取默认配置
    retryDelay: 0,
  })

  await new Promise<void>((resolve) => {
    const unsubscribe = observer.subscribe((result) => {
      if (result.isError || result.isSuccess) {
        unsubscribe()
        resolve()
      }
    })
  })

  client.clear()
  return queryFn.mock.calls.length
}

it('401 不重试: 只发一次请求 (别把「未登录」重试成 4 次)', async () => {
  expect(await attemptsFor(new ApiError('未登录或会话已过期', 401))).toBe(1)
})

it('403 不重试: 普通用户点到管理员功能也不放大成 4 次', async () => {
  // 文案里不含任何状态码字样 —— 走的是状态码判断, 不是文案匹配
  expect(await attemptsFor(new ApiError('无权执行该操作', 403))).toBe(1)
})

it('其余错误保持默认重试语义 (1 + 3 次)', async () => {
  expect(await attemptsFor(new Error('500 Internal Server Error'))).toBe(4)
})
