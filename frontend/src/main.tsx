import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { initializeFrontendExtensions } from './extensions/bootstrap'
import { createQueryClient } from './lib/queryClient'
import './index.css'

// 全局认证拦截: 任何 query/mutation 收到 401 (未登录/会话过期) → 跳登录页。
// api.ts 的 request() 已对 401 静默 (不弹 toast), 这里统一负责跳转。
// 排除 /login 自身的请求, 避免登录页请求失败又跳登录形成死循环。
//
// ``redirecting`` 只在**一次整页导航内**有效: 它防的是「同一次页面生命周期里 N 个
// 请求同时 401 → N 次跳转」的重复跳转。下面的 ``window.location.href`` 是整页导航,
// 页面卸载后模块重载、标志随新模块复位 —— 这是有意的, 不是「拦截器只生效一次」的
// 缺陷: 若标志跨导航保留, 新页面里的 401 (会话再次过期) 就再也跳不动登录页了。
const _redirectToLogin = (() => {
  let redirecting = false
  return (err: unknown) => {
    if (redirecting) return
    if (!(err instanceof Error)) return
    const msg = err.message || ''
    // 401 (未登录/会话过期) → 跳登录页
    // 403 未初始化 (面板未设密码, 公网访问) → 也跳登录页(显示设密码提示)
    const is401 = msg.includes('未登录') || msg.includes('会话已过期') || msg.includes('401')
    const isNotInit = msg.includes('尚未初始化访问密码') || msg.includes('NOT_INITIALIZED')
    if (!is401 && !isNotInit) return
    // 已在登录页则不跳(避免死循环)
    if (window.location.pathname === '/login') return
    redirecting = true
    const redirect = encodeURIComponent(window.location.pathname + window.location.search)
    window.location.href = `/login?redirect=${redirect}`
  }
})()

// 客户端与失败重试策略在 lib/queryClient.ts (可测): 401/403 不重试, 其余保持默认 3 次
const queryClient = createQueryClient(_redirectToLogin)

async function bootstrap() {
  await initializeFrontendExtensions()
  const { router } = await import('./router')
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </React.StrictMode>,
  )
}

void bootstrap()
