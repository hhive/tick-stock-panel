/**
 * 访问认证页 — 多用户账号入口, 同时保留旧的「访问密码」通道。
 *
 * 三态:
 *   - 主操作「从 Sub2API 一键进入」: 去 Sub2API 站点, 用它的导航菜单带 ?apikey= 跳回本面板
 *     (凭证由 JumpGate 摘取并换取会话)。
 *   - 邮箱 + 密码 注册 / 登录(次选)。
 *   - 访问密码(旧单密码模式) — 首次设密码需本机/内网, 公网会被 403 拒绝, 页面据此提示。
 *
 * 安全:
 *   - 登录失败由后端限流(5次锁5分钟), 429 时前端显示等待提示。
 *   - 跳转凭证只经 URL 传入一次, 存内存不落盘(见 lib/account.ts)。
 */
import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import {
  Eye, EyeOff, ExternalLink, Loader2, Lock, Mail, ShieldCheck, ShieldAlert, Sparkles, UserPlus,
} from 'lucide-react'
import { api } from '@/lib/api'
import { Logo } from '@/components/Logo'
import { cn } from '@/lib/cn'
import { SUB2API_SITE_URL, getHeldJumpKey, setHeldJumpKey } from '@/lib/account'

/** login/register = 邮箱密码; legacy = 旧的单访问密码通道 */
type AuthMode = 'login' | 'register' | 'legacy'

/** 与后端一致: 只要求 @ 两侧非空且无空白 —— 过度严格的正则会挡住内网域名等合法地址 */
const EMAIL_RE = /^\S+@\S+$/

function emailErrorText(err: unknown, mode: 'login' | 'register'): string {
  const status = (err as { status?: number } | null)?.status
  const raw = err instanceof Error ? (err.message || '').trim() : ''
  // 后端没给 detail 时 api 层会兜底成 "429 Too Many Requests" 这类英文, 丢掉换成中文提示
  const server = raw && !/^\d{3}\s/.test(raw) ? raw : ''
  if (status === 429) return server || '操作过于频繁, 请稍后再试'
  if (status === 409) return server || '该邮箱已被注册, 请直接登录'
  if (status === 401) return server || '邮箱或密码错误'
  return server || (mode === 'register' ? '注册失败, 请稍后再试' : '登录失败, 请稍后再试')
}

export function Auth() {
  const navigate = useNavigate()
  // 是否带着 Sub2API 跳转凭证进来(带则默认引导注册, 并提示会自动绑定)
  const [heldKey, setHeldKey] = useState<string | null>(() => getHeldJumpKey())
  const [mode, setMode] = useState<AuthMode>(() => (getHeldJumpKey() ? 'register' : 'login'))
  const [email, setEmail] = useState('')
  const [pwd, setPwd] = useState('')
  const [pwd2, setPwd2] = useState('')
  const [password, setPassword] = useState('')          // legacy 通道
  const [confirmPassword, setConfirmPassword] = useState('')  // 仅设密码时用
  const [showPwd, setShowPwd] = useState(false)
  const [localError, setLocalError] = useState('')

  // 取认证状态(是否已设密码)
  const [status, setStatus] = useState<{ configured: boolean } | null>(null)
  useEffect(() => {
    api.authStatus().then(s => {
      setStatus(s)
      // 已登录的话直接进面板(避免登录页死循环)
      if (s.authenticated) navigate('/', { replace: true })
    }).catch(() => setStatus({ configured: false }))
  }, [navigate])

  const isSetup = !status?.configured  // configured=false → 设密码模式
  const isLegacy = mode === 'legacy'

  /** 登录/注册成功后的收尾: 绑定跳转凭证(若有) → 进入面板 */
  const enterApp = async () => {
    const pending = getHeldJumpKey()
    if (pending) {
      try {
        await api.accountBind(pending)
      } catch {
        // 绑定失败不阻塞进入面板: 账号本身已登录成功, 失败原因由 api 层 toast 提示
      } finally {
        setHeldJumpKey(null)
        setHeldKey(null)
      }
    }
    // 成功: 跳回原页面(或首页)
    const redirect = new URLSearchParams(window.location.search).get('redirect') || '/'
    navigate(redirect, { replace: true })
  }

  // 邮箱密码: 注册 / 登录
  const emailMut = useMutation({
    mutationFn: async () => {
      if (mode === 'register') return api.accountRegister(email.trim(), pwd)
      return api.accountLogin(email.trim(), pwd)
    },
    onSuccess: () => { void enterApp() },
    onError: (err: unknown) => {
      setLocalError(emailErrorText(err, mode === 'register' ? 'register' : 'login'))
    },
  })

  // 访问密码(旧通道): 设密码 / 登录
  const submitMut = useMutation({
    mutationFn: async () => {
      if (isSetup) {
        return api.authSetup(password)
      }
      return api.authLogin(password)
    },
    onSuccess: () => { void enterApp() },
    onError: (err: any) => {
      const msg = err?.message || (isSetup ? '设置失败' : '登录失败')
      // 设密码/登录失败必须显示: 401(密码错)/403(公网设密码被拒)/429(限流) 都要提示
      setLocalError(msg)
    },
  })

  const switchMode = (next: AuthMode) => {
    setMode(next)
    setLocalError('')
  }

  const handleEmailSubmit = (e: FormEvent) => {
    e.preventDefault()
    setLocalError('')
    if (!EMAIL_RE.test(email.trim())) { setLocalError('请输入正确的邮箱地址'); return }
    if (pwd.length < 6) { setLocalError('密码至少 6 位'); return }
    if (mode === 'register' && pwd !== pwd2) { setLocalError('两次密码不一致'); return }
    emailMut.mutate()
  }

  const handleLegacySubmit = (e: FormEvent) => {
    e.preventDefault()
    setLocalError('')
    if (isSetup) {
      if (password.length < 6) { setLocalError('密码至少 6 位'); return }
      if (password !== confirmPassword) { setLocalError('两次密码不一致'); return }
    }
    submitMut.mutate()
  }

  if (!status) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <Loader2 className="h-6 w-6 animate-spin text-muted" />
      </div>
    )
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-base px-4">
      {/* 背景辉光(与 Onboarding 风格一致) */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(139,92,246,0.15),transparent_40%),radial-gradient(circle_at_70%_80%,rgba(59,130,246,0.12),transparent_40%)]" />

      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-full max-w-sm"
      >
        {/* Logo */}
        <div className="mb-6 flex flex-col items-center gap-2">
          <Logo className="h-10 w-10" />
          <h1 className="text-lg font-semibold text-foreground">Tick Stock Panel</h1>
        </div>

        <div className="rounded-card border border-border bg-surface/90 p-6 shadow-2xl backdrop-blur">
          {/* 标题区: 图标 + 文案随模式切换 */}
          <div className="mb-5 flex items-center gap-2.5">
            <div className={cn(
              'grid h-9 w-9 place-items-center rounded-lg',
              isLegacy
                ? (isSetup ? 'bg-accent/15 text-accent' : 'bg-purple-500/15 text-purple-400')
                : 'bg-purple-500/15 text-purple-400',
            )}>
              {isLegacy
                ? (isSetup ? <ShieldCheck className="h-5 w-5" /> : <Lock className="h-5 w-5" />)
                : (mode === 'register' ? <UserPlus className="h-5 w-5" /> : <Lock className="h-5 w-5" />)}
            </div>
            <div>
              <div className="text-sm font-medium text-foreground">
                {isLegacy
                  ? (isSetup ? '设置访问密码' : '登录访问')
                  : (mode === 'register' ? '注册账号' : '登录')}
              </div>
              <div className="text-[11px] text-muted">
                {isLegacy
                  ? (isSetup ? '首次使用, 请为面板设置访问密码' : '请输入访问密码以继续')
                  : (mode === 'register' ? '创建你的面板账号' : '使用 Sub2API 跳转, 或用邮箱密码登录')}
              </div>
            </div>
          </div>

          {/* 主操作: 去 Sub2API 用其导航菜单跳回本面板 */}
          <a
            data-testid="sub2api-enter"
            href={SUB2API_SITE_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90"
          >
            <ExternalLink className="h-4 w-4" />
            从 Sub2API 一键进入
          </a>
          <p className="mt-2 text-[10px] leading-relaxed text-muted/70">
            打开 Sub2API 后, 从站点导航菜单选择「跳转到行情面板」, 即可自动登录本面板。
          </p>

          {/* 分隔 */}
          <div className="my-4 flex items-center gap-3 text-[10px] text-muted/60">
            <span className="h-px flex-1 bg-border" />
            或
            <span className="h-px flex-1 bg-border" />
          </div>

          {/* 已持有跳转凭证: 注册/登录后自动绑定 */}
          {heldKey && (
            <div
              data-testid="jump-held-hint"
              className="mb-3 flex items-start gap-1.5 rounded-btn bg-accent/10 px-3 py-2 text-[11px] text-accent"
            >
              <Sparkles className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>已收到 Sub2API 跳转凭证, 注册或登录后将自动绑定该 API Key。</span>
            </div>
          )}

          {isLegacy ? (
            /* ===== 旧通道: 单访问密码 ===== */
            <form onSubmit={handleLegacySubmit} className="space-y-3">
              <div className="relative">
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="访问密码"
                  autoFocus
                  className="h-10 w-full rounded-btn border border-border bg-base px-3 pr-9 text-sm text-foreground outline-none transition-colors focus:border-accent/50"
                />
                <button
                  type="button"
                  onClick={() => setShowPwd(s => !s)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-foreground"
                  tabIndex={-1}
                >
                  {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>

              {isSetup && (
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  placeholder="再次输入密码"
                  className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors focus:border-accent/50"
                />
              )}

              {localError && (
                <div className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-[11px] text-danger">
                  <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
                  <span>{localError}</span>
                </div>
              )}

              <button
                type="submit"
                data-testid="legacy-submit"
                disabled={submitMut.isPending || !password}
                className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {submitMut.isPending ? (
                  <><Loader2 className="h-4 w-4 animate-spin" />处理中…</>
                ) : (
                  <>{isSetup ? '设置并进入' : '登录'}</>
                )}
              </button>
            </form>
          ) : (
            /* ===== 邮箱 + 密码 ===== */
            <div>
              {/* 登录 / 注册 切换 */}
              <div className="mb-3 grid grid-cols-2 gap-1 rounded-btn bg-base p-1">
                {(['login', 'register'] as const).map(m => (
                  <button
                    key={m}
                    type="button"
                    data-testid={`mode-${m}`}
                    onClick={() => switchMode(m)}
                    className={cn(
                      'h-7 rounded-[6px] text-xs transition-colors',
                      mode === m ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-foreground',
                    )}
                  >
                    {m === 'login' ? '登录' : '注册'}
                  </button>
                ))}
              </div>

              {/* noValidate: 交给下面的中文校验, 避免浏览器原生气泡(英文/随语言环境)盖掉提示 */}
              <form onSubmit={handleEmailSubmit} noValidate className="space-y-3">
                <div className="relative">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted/60" />
                  <input
                    data-testid="email-input"
                    type="email"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    placeholder="邮箱"
                    autoComplete="username"
                    autoFocus
                    className="h-10 w-full rounded-btn border border-border bg-base pl-9 pr-3 text-sm text-foreground outline-none transition-colors focus:border-accent/50"
                  />
                </div>

                <div className="relative">
                  <input
                    data-testid="password-input"
                    type={showPwd ? 'text' : 'password'}
                    value={pwd}
                    onChange={e => setPwd(e.target.value)}
                    placeholder={mode === 'register' ? '设置密码(至少 6 位)' : '密码'}
                    autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
                    className="h-10 w-full rounded-btn border border-border bg-base px-3 pr-9 text-sm text-foreground outline-none transition-colors focus:border-accent/50"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPwd(s => !s)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted hover:text-foreground"
                    tabIndex={-1}
                  >
                    {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>

                {mode === 'register' && (
                  <input
                    data-testid="password2-input"
                    type={showPwd ? 'text' : 'password'}
                    value={pwd2}
                    onChange={e => setPwd2(e.target.value)}
                    placeholder="再次输入密码"
                    autoComplete="new-password"
                    className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors focus:border-accent/50"
                  />
                )}

                {/* 错误提示 (含 401/409/429) */}
                {localError && (
                  <div
                    data-testid="auth-error"
                    className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-[11px] text-danger"
                  >
                    <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
                    <span>{localError}</span>
                  </div>
                )}

                <button
                  type="submit"
                  data-testid="email-submit"
                  disabled={emailMut.isPending || !email || !pwd}
                  className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
                >
                  {emailMut.isPending ? (
                    <><Loader2 className="h-4 w-4 animate-spin" />处理中…</>
                  ) : (
                    <>{mode === 'register' ? '注册并进入' : '登录'}</>
                  )}
                </button>
              </form>
            </div>
          )}

          {/* 旧单密码通道: 默认折叠为一行链接, 保持原有能力可用 */}
          <div className="mt-4 text-center">
            <button
              type="button"
              data-testid="toggle-legacy"
              onClick={() => switchMode(isLegacy ? (heldKey ? 'register' : 'login') : 'legacy')}
              className="text-[11px] text-muted underline-offset-2 transition-colors hover:text-foreground hover:underline"
            >
              {isLegacy
                ? '返回邮箱登录'
                : (isSetup ? '首次使用? 设置访问密码' : '使用访问密码登录')}
            </button>
          </div>

          {/* 提示: 设密码模式告知本机限制 */}
          {isLegacy && isSetup && (
            <div className="mt-3 space-y-1.5 text-[10px] leading-relaxed text-muted/70">
              <p>
                出于安全考虑, 首次设置密码需在服务器本机或内网访问时操作。公网环境下仅可登录。
              </p>
              <p>
                详细配置说明见{' '}
                <a
                  href="https://github.com/shy3130/tickflow-stock-panel/blob/main/docs/deploy-password.md"
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent underline-offset-2 hover:underline"
                >
                  访问密码部署文档
                </a>
              </p>
            </div>
          )}
        </div>

        <div className="mt-4 flex items-center justify-center gap-1.5 text-[10px] text-muted/60">
          <Sparkles className="h-3 w-3" />
          自托管量化工作台 · 数据完全掌握在自己手里
        </div>
      </motion.div>
    </div>
  )
}
