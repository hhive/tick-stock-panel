/**
 * 启动跳转门 — 处理 Sub2API 带 ``?apikey=`` 跳回本面板的场景。
 *
 * 流程: 摘掉地址栏里的凭证 → POST /api/account/jump →
 *   logged_in   → 放行, 直接进面板 (会话 cookie 已由后端下发)
 *   needs_auth  → 转登录页, 凭证留在内存, 注册/登录成功后绑定
 *   401         → 凭证无效, 提示「API Key 无效或已失效」
 *   网络/服务端异常 → 显示可重试的错误, 不静默吞掉
 *
 * 挂载位置是路由树最外层的 pathless 布局路由, 所以任何入口路径都先过这道门。
 */
import { useEffect, useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Loader2, RotateCw, ShieldAlert, TriangleAlert } from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { Logo } from '@/components/Logo'
import { captureJumpKeyFromUrl, getHeldJumpKey, setHeldJumpKey } from '@/lib/account'

type JumpState =
  | { kind: 'checking' }
  | { kind: 'proceed' }
  | { kind: 'needs_auth' }
  | { kind: 'invalid_key' }
  | { kind: 'failed'; message: string }

/** 同一页面生命周期内只真正发一次跳转请求 (React 严格模式会重复挂载副作用) */
let _inflight: Promise<JumpState> | null = null

function jumpErrorText(err: unknown): string {
  const msg = err instanceof Error ? (err.message || '').trim() : ''
  // fetch 的裸网络错误文案各浏览器不同, 统一翻译成可操作提示
  if (!msg || /failed to fetch|network ?error|load failed/i.test(msg)) {
    return '网络异常, 请检查网络后重试'
  }
  return msg
}

async function runJump(): Promise<JumpState> {
  // 地址栏已由渲染期摘掉; 这里再兜一次以防直接调用, 并覆盖「重试」路径(此时只有内存里有 key)
  const key = captureJumpKeyFromUrl() ?? getHeldJumpKey()
  if (!key) return { kind: 'proceed' }
  try {
    const res = await api.accountJump(key)
    if (res.status === 'logged_in') {
      setHeldJumpKey(null)  // 后端已建立绑定, 内存里不再留凭证
      return { kind: 'proceed' }
    }
    // key 有效但本面板还没有对应账号: 留在内存等注册/登录后绑定
    return { kind: 'needs_auth' }
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      setHeldJumpKey(null)  // 失效凭证没有保留价值
      return { kind: 'invalid_key' }
    }
    return { kind: 'failed', message: jumpErrorText(err) }
  }
}

function startJump(): Promise<JumpState> {
  if (!_inflight) _inflight = runJump()
  return _inflight
}

/** 仅测试用: 复位模块级状态 (生产代码不要调用)。 */
export function resetJumpStateForTests(): void {
  _inflight = null
}

/** 跳转过程中的全屏壳 (与 Auth / Onboarding 同一套视觉) */
function JumpShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-base px-4">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(139,92,246,0.15),transparent_40%),radial-gradient(circle_at_70%_80%,rgba(59,130,246,0.12),transparent_40%)]" />
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-full max-w-sm"
      >
        <div className="mb-6 flex flex-col items-center gap-2">
          <Logo className="h-10 w-10" />
          <h1 className="text-lg font-semibold text-foreground">Tick Stock Panel</h1>
        </div>
        <div className="rounded-card border border-border bg-surface/90 p-6 text-center shadow-2xl backdrop-blur">
          {children}
        </div>
      </motion.div>
    </div>
  )
}

export function JumpGate() {
  const navigate = useNavigate()
  const [state, setState] = useState<JumpState>(() => {
    // 凭证必须在首帧渲染前就摘掉: 它是 bearer 凭证, 不能留在地址栏/浏览历史里,
    // 也不能被后续请求带进 Referer。摘取是同步的, 早于任何 await。
    const captured = captureJumpKeyFromUrl()
    return captured || getHeldJumpKey() ? { kind: 'checking' } : { kind: 'proceed' }
  })

  useEffect(() => {
    if (state.kind === 'checking') {
      let alive = true
      void startJump().then(next => { if (alive) setState(next) })
      return () => { alive = false }
    }
    if (state.kind === 'needs_auth') {
      // 转登录页: 这里必须用 navigate 而不是渲染 <Navigate/> —— 本组件是布局路由,
      // 渲染 <Navigate/> 会占住出口(它渲染 null), 子路由永远挂不上。
      // 回跳地址用 window.location 而非 useLocation: 地址栏已被 replaceState 改过,
      // 路由内部还不知道, 而 redirect 必须回传「去掉凭证之后」的地址。
      const back = window.location.pathname + window.location.search
      navigate(`/login?redirect=${encodeURIComponent(back)}`, { replace: true })
      setState({ kind: 'proceed' })
    }
  }, [state.kind, navigate])

  const retry = () => {
    _inflight = null
    setState({ kind: 'checking' })
  }

  // proceed: 放行; checking / needs_auth: 转场过程(needs_auth 会在同一批更新里落到 proceed)
  if (state.kind === 'proceed') return <Outlet />
  if (state.kind === 'checking' || state.kind === 'needs_auth') {
    return (
      <JumpShell>
        <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin text-muted" />
        <div className="text-sm text-foreground">正在验证 Sub2API 凭证…</div>
        <div className="mt-1 text-[11px] text-muted">请稍候, 正在为你建立面板会话</div>
      </JumpShell>
    )
  }

  if (state.kind === 'invalid_key') {
    return (
      <JumpShell>
        <ShieldAlert className="mx-auto mb-3 h-6 w-6 text-danger" />
        <div className="text-sm font-medium text-foreground">API Key 无效或已失效</div>
        <div className="mt-1.5 text-[11px] leading-relaxed text-muted">
          该跳转凭证无法通过 Sub2API 校验, 请回到 Sub2API 重新获取 API Key, 或直接用邮箱密码登录本面板。
        </div>
        <button
          type="button"
          data-testid="jump-relogin"
          onClick={() => navigate('/login', { replace: true })}
          className="mt-4 inline-flex h-9 w-full items-center justify-center rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90"
        >
          重新登录
        </button>
      </JumpShell>
    )
  }

  return (
    <JumpShell>
      <TriangleAlert className="mx-auto mb-3 h-6 w-6 text-danger" />
      <div className="text-sm font-medium text-foreground">跳转验证失败</div>
      <div className="mt-1.5 text-[11px] leading-relaxed text-muted">{state.message}</div>
      <div className="mt-4 flex gap-2">
        <button
          type="button"
          data-testid="jump-retry"
          onClick={retry}
          className="inline-flex h-9 flex-1 items-center justify-center gap-1.5 rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90"
        >
          <RotateCw className="h-3.5 w-3.5" />重试
        </button>
        <button
          type="button"
          data-testid="jump-relogin"
          onClick={() => navigate('/login', { replace: true })}
          className="inline-flex h-9 flex-1 items-center justify-center rounded-btn border border-border text-sm text-foreground transition-colors hover:bg-surface"
        >
          重新登录
        </button>
      </div>
    </JumpShell>
  )
}
