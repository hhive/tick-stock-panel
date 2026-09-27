/**
 * 侧边栏账号入口 —— 显示当前账号并提供退出; 未登录时给出登录入口。
 *
 * 与同区的「数据源」「AI 配置」两行同属状态卡一族: 2px 竖条 + 图标 + 小字 + 状态点。
 * 竖条刻意取中性色 —— 绿/紫/琥珀已被数据源 / AI / 告警占用, 账号不该再抢一个语义色,
 * 否则侧栏颜色就失去可读性。
 *
 * 数据直接取 `/api/auth/status`(已有 email / role / mode), 不调 `/api/account/me` ——
 * 后者对单密码应急会话会 401, 而这里恰恰要能显示应急会话的身份。
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, LogOut, LogIn, UserRound } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useSignOut } from '@/lib/useSignOut'

export function AccountBadge() {
  const navigate = useNavigate()
  const { signOut, signingOut } = useSignOut()
  const [menuOpen, setMenuOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement | null>(null)

  const { data: status } = useQuery({
    queryKey: QK.authStatus,
    queryFn: api.authStatus,
    staleTime: 30_000,
  })

  // 点击外部 / Esc 关闭菜单
  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  // 状态未知时什么都不渲染 —— 先显示「未登录」再跳成邮箱会闪一下, 比晚半拍更糟
  if (!status) return null

  const signedIn = status.authenticated
  const label = status.mode === 'legacy'
    ? '应急入口'
    : (status.email || '已登录')

  return (
    <div ref={boxRef} className="relative">
      <button
        type="button"
        onClick={() => (signedIn ? setMenuOpen(v => !v) : navigate('/login'))}
        aria-haspopup={signedIn ? 'menu' : undefined}
        aria-expanded={signedIn ? menuOpen : undefined}
        title={signedIn ? `${label} — 点击管理账号` : '登录或注册面板账号'}
        className="group relative flex w-full items-center gap-2 overflow-hidden rounded-md py-1.5 pl-2.5 pr-2 text-left transition-colors duration-150 hover:bg-elevated/70"
      >
        <span className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-border group-hover:bg-muted" />
        {signedIn
          ? <UserRound className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-foreground transition-colors" />
          : <LogIn className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-foreground transition-colors" />}

        {signedIn ? (
          <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-secondary group-hover:text-foreground transition-colors">
            {label}
          </span>
        ) : (
          <>
            <span className="text-[11px] text-secondary group-hover:text-foreground transition-colors">未登录</span>
            <span className="ml-auto text-[11px] font-medium text-accent">登录</span>
          </>
        )}

        {/* 用 accent 而不是 bull/bear: 本盘是中式配色(红=涨绿=跌), 拿红点标「已登录」
            读起来像故障告警。accent 蓝在侧栏已表示「当前所在」, 语义一致。 */}
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${signedIn ? 'bg-accent' : 'bg-muted/40'}`} />
      </button>

      {menuOpen && (
        <div
          role="menu"
          className="absolute left-1 right-1 top-full z-30 mt-1 overflow-hidden rounded-lg border border-border/60 bg-surface shadow-lg"
        >
          {/* 身份完整地放在菜单里: 侧栏那一行要留给邮箱本身(截断的邮箱认不出是谁) */}
          <div className="border-b border-border/50 px-3 py-1.5">
            <div className="truncate text-[11px] text-foreground">{label}</div>
            <div className="mt-0.5 text-[10px] text-muted">
              {status.mode === 'legacy' ? '单密码应急入口' : (status.role === 'admin' ? '管理员' : '普通用户')}
            </div>
          </div>
          {status.mode === 'account' && (
            <button
              type="button"
              role="menuitem"
              onClick={() => { setMenuOpen(false); navigate('/settings?tab=system') }}
              className="block w-full px-3 py-1.5 text-left text-xs text-secondary transition-colors hover:bg-elevated/70 hover:text-foreground"
            >
              系统设置
            </button>
          )}
          <button
            type="button"
            role="menuitem"
            disabled={signingOut}
            onClick={() => { setMenuOpen(false); void signOut(status.mode) }}
            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs text-secondary transition-colors hover:bg-elevated/70 hover:text-danger disabled:opacity-50"
          >
            {signingOut ? <Loader2 className="h-3 w-3 animate-spin" /> : <LogOut className="h-3 w-3" />}
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}
