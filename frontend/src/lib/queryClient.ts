/**
 * React Query 客户端 —— 全局错误处理与失败重试策略。
 *
 * 单独成模块而不是写在 main.tsx 里: main.tsx 一旦被 import 就自举渲染, 没法在测试里
 * 钉住这里的策略 —— 而这条策略写错的后果是性能问题(多打 3 次必然失败的请求、游客白等
 * 约 7 秒), 只看代码看不出来, 必须测。
 */
import { QueryClient, QueryCache } from '@tanstack/react-query'

/**
 * 「重试它没有意义」的状态码 —— 认证/授权失败:
 *   - 401 未登录/会话已过期: 再试一次不会变通过;
 *   - 403 无权/未初始化: 普通用户点到管理员功能时尤其要排, 否则每次点都白打 3 次。
 * 按**状态码**判断 (ApiError.status), 不按文案。
 */
const NO_RETRY_STATUSES = new Set([401, 403])

/**
 * 查询失败重试策略: 除认证/授权失败外, 保持 React Query 默认语义 (最多 3 次重试)。
 *
 * 为什么排除 401/403: 默认 retry=3 + 退避 1s/2s/4s 会把每个失败请求放大成 4 次。游客
 * 打开面板时, 十余个受保护端点会各多打 3 个必然 401 的请求, 而且必须等约 7 秒才被弹到
 * 登录页(拦截器挂在重试耗尽后的 onError 上) —— 这是 D2 里「抖动 + 白耗限流」的实质来源。
 */
function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status
  if (typeof status === 'number' && NO_RETRY_STATUSES.has(status)) return false
  return failureCount < 3
}

/** 建全局 query 客户端; onError 由调用方注入(全局认证拦截器见 main.tsx)。 */
export function createQueryClient(onError: (err: unknown) => void): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (err) => onError(err),
    }),
    defaultOptions: {
      queries: {
        staleTime: 5_000,           // 5s 内复用,与 §4.2 Repository 不变量一致
        refetchOnWindowFocus: false,
        retry: shouldRetryQuery,
      },
      mutations: {
        onError: (err) => onError(err),
      },
    },
  })
}
