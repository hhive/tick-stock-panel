import { useState, useEffect, useRef, createContext, useContext } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Save, Loader2, Check, Wifi, WifiOff, Eye, EyeOff, Shield,
  Shuffle, Plug, Settings2, Trash2, ChevronDown,
} from 'lucide-react'
import { useSettings } from '@/lib/useSharedQueries'
import { api, type SettingsState } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCardFlash, cardFlashCls } from '@/lib/useCardFlash'

// 统一的输入框样式(与项目其他设置页一致)
const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs font-mono text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow'

// 空/非法输入 → undefined (后端保持原值), 合法正整数 → int
const toPositiveInt = (v: string) => {
  const n = parseInt(v, 10)
  return Number.isInteger(n) && n > 0 ? n : undefined
}

// ── AI 上游锁定 ────────────────────────────────────────────────
// 本站 AI 配置只有「自定义」一种形态: 走本站 Sub2API 网关。地址由服务端强制写入
// (`backend/app/config.py` 的 AI_GATEWAY_BASE_URL), 前端这里只负责**展示**。
//
// 显示以服务端返回的 ai_base_url 为准 —— 万一后端的常量改了而这里忘了改, 界面会
// 如实显示请求实际打向哪里, 而不是拿本地常量把差异盖住。下面的常量只是服务端还没
// 返回时的兜底, 不要当成权威值。
const AI_GATEWAY_BASE_URL = 'https://xiaoni-model.top/v1'
const OPENAI_COMPAT_PROVIDER = 'openai_compat'

export function SettingsAIPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const settings = useSettings()
  const s = settings.data

  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [customUa, setCustomUa] = useState(false)
  const [userAgent, setUserAgent] = useState('')
  const [maxOutputTokens, setMaxOutputTokens] = useState('')
  const [contextWindow, setContextWindow] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  // 模型列表下拉（从本站网关 /v1/models 拉取该 key 可见的模型 + 关键词过滤）
  const [modelsOpen, setModelsOpen] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState('')
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [modelFilter, setModelFilter] = useState('')
  const modelBoxRef = useRef<HTMLDivElement | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  const configured = s?.ai_configured ?? s?.has_ai_key ?? false
  // 服务端返回优先: 地址栏必须显示请求**实际**打向哪里。本地常量只在首屏(或服务端
  // 返回空值)时兜底, 不能反过来盖住服务端的值。
  const baseUrl = s?.ai_base_url || AI_GATEWAY_BASE_URL
  const canSave = !!model.trim()

  useEffect(() => {
    if (!s) return
    // 未配置过 AI (无 api_key): 字段留空, 不预填充后端默认值
    const unconfigured = !s.has_ai_key && !s.ai_configured
    setModel(unconfigured ? '' : (s.ai_model ?? ''))
    const ua = s.ai_user_agent ?? ''
    setCustomUa(!!ua)
    setUserAgent(ua)
    setMaxOutputTokens(String(s?.ai_max_output_tokens ?? 16384))
    setContextWindow(String(s?.ai_context_window ?? 128000))
  }, [s])

  const payload = () => ({
    provider: OPENAI_COMPAT_PROVIDER,
    // 服务端会强制覆盖这个值(见 backend/app/api/settings.py), 这里传常量只是让
    // 请求体自洽; 真正的锁在服务端, 用户改不了
    base_url: AI_GATEWAY_BASE_URL,
    api_key: apiKey || undefined,
    model,
    user_agent: customUa ? userAgent : '',
    max_output_tokens: toPositiveInt(maxOutputTokens),
    context_window: toPositiveInt(contextWindow),
  })

  const save = useMutation({
    mutationFn: () => api.saveAiSettings(payload()),
    onSuccess: (result) => {
      setSaved(true)
      setApiKey('')
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: result.ai_provider ?? OPENAI_COMPAT_PROVIDER,
        ai_base_url: result.ai_base_url ?? AI_GATEWAY_BASE_URL,
        ai_model: result.ai_model ?? model,
        ai_openai_model: result.ai_openai_model ?? model,
        ai_configured: result.ai_configured ?? (apiKey ? true : prev.ai_configured),
        ai_max_output_tokens: result.ai_max_output_tokens ?? toPositiveInt(maxOutputTokens),
        ai_context_window: result.ai_context_window ?? toPositiveInt(contextWindow),
        ...(apiKey ? {
          has_ai_key: true,
          ai_api_key_masked: `${apiKey.slice(0, 4)}......${apiKey.slice(-4)}`,
        } : {}),
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
      setTimeout(() => setSaved(false), 2000)
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearAiSettings(),
    onSuccess: () => {
      setConfirmClear(false)
      setApiKey('')
      setModel('')
      setMaxOutputTokens('16384')
      setContextWindow('128000')
      setTestResult(null)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: OPENAI_COMPAT_PROVIDER,
        ai_base_url: AI_GATEWAY_BASE_URL,
        ai_model: '',
        ai_openai_model: '',
        ai_max_output_tokens: 16384,
        ai_context_window: 128000,
        has_ai_key: false,
        ai_configured: false,
        ai_api_key_masked: '',
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
    },
  })

  const genRandomUa = () => {
    const major = 128 + Math.floor(Math.random() * 8)
    const platforms = [
      'Windows NT 10.0; Win64; x64',
      'Macintosh; Intel Mac OS X 10_15_7',
      'X11; Linux x86_64',
    ]
    const pf = platforms[Math.floor(Math.random() * platforms.length)]
    setUserAgent(`Mozilla/5.0 (${pf}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`)
  }

  const handleModelChange = (value: string) => setModel(value)

  // 经后端代理拉取本站网关上该 key 可见的模型列表
  // (浏览器直连会把 key 暴露在跨域请求里; 且 Sub2API 按用户分组过滤, 必须带用户自己的 key)
  const fetchModelOptions = async () => {
    setModelsLoading(true)
    setModelsError('')
    setModelFilter('')
    setModelsOpen(true)
    try {
      const data = await api.aiModels(apiKey || undefined)
      setModelOptions(data.models)
    } catch (error) {
      setModelOptions([])
      setModelsError(error instanceof Error ? error.message : '获取模型列表失败')
    } finally {
      setModelsLoading(false)
    }
  }

  const filteredModelOptions = modelFilter.trim()
    ? modelOptions.filter(id => id.toLowerCase().includes(modelFilter.trim().toLowerCase()))
    : modelOptions

  // 下拉打开时点击外部关闭
  useEffect(() => {
    if (!modelsOpen) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (modelBoxRef.current && !modelBoxRef.current.contains(e.target as Node)) setModelsOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [modelsOpen])

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      if (canSave) await api.saveAiSettings(payload())
      const r = await api.strategyAiTest()
      setTestResult({ ok: r.ok, msg: r.ok ? `连通成功 · ${r.model ?? model}` : (r.error ?? '未知错误') })
    } catch (e: any) {
      setTestResult({ ok: false, msg: String(e?.message ?? '测试失败') })
    } finally {
      setTesting(false)
    }
  }

  return (
    <HighlightContext.Provider value={highlight ?? ''}>
    <div className="space-y-5 max-w-2xl">
      <Card icon={Plug} title="连接状态" anchor="ai-connection" right={
        configured && (
          <button onClick={handleTest} disabled={testing}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated hover:bg-elevated/80 text-xs text-secondary transition-colors duration-150 ease-smooth disabled:opacity-50">
            {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
            {testing ? '测试中' : '测试'}
          </button>
        )
      }>
        <div className="flex items-center gap-3">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${configured ? 'bg-emerald-400/10 text-emerald-400' : 'bg-amber-400/10 text-amber-400'}`}>
            {configured ? <Wifi className="h-4.5 w-4.5" /> : <WifiOff className="h-4.5 w-4.5" />}
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">{configured ? 'AI 已连接' : 'AI 未配置'}</div>
            <div className="text-xs text-muted mt-0.5 truncate">
              {configured
                ? `${s?.ai_model} · ${s?.ai_api_key_masked}`
                : '填入 Sub2API 的 API Key 后即可使用 AI 功能。'}
            </div>
          </div>
        </div>
        {testResult && (
          <div className={`mt-3 rounded-btn border px-3 py-2 text-xs flex items-center gap-2 ${testResult.ok ? 'border-emerald-400/20 bg-emerald-400/[0.04] text-emerald-400' : 'border-danger/20 bg-danger/[0.04] text-danger'}`}>
            <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${testResult.ok ? 'bg-emerald-400' : 'bg-danger'}`} />
            {testResult.msg}
          </div>
        )}
      </Card>

      <Card
        icon={Settings2}
        title="自定义配置"
        right={
          <span className="inline-flex items-center gap-1.5 text-[10px] text-muted/60" title="Use OpenAI-compatible Chat Completions API">
            <span className="rounded-full border border-border/40 bg-base/50 px-1.5 py-px font-mono">Chat Completions</span>
            接口
          </span>
        }
      >
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="API 地址" hint="本站 AI 网关，固定不可修改。">
              <input type="text" value={baseUrl} readOnly aria-label="AI 网关地址" className={`${INPUT_CLS} cursor-default text-muted/80`} />
            </Field>
            <Field label="模型" hint="可从本站网关拉取可用列表，也可直接填写">
              <div ref={modelBoxRef} className="relative">
                <input type="text" value={model} onChange={e => handleModelChange(e.target.value)} placeholder="gpt-5.6-sol" className={`${INPUT_CLS} pr-9`} />
                <button type="button" onClick={fetchModelOptions} aria-label="获取模型列表"
                  className="absolute right-1.5 top-1/2 flex h-6 w-7 -translate-y-1/2 items-center justify-center rounded-md border border-border/40 bg-base text-muted transition-colors hover:border-accent/40 hover:text-accent">
                  {modelsLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronDown className="h-3.5 w-3.5" />}
                </button>
                {modelsOpen && (
                  <div className="absolute z-20 mt-1 w-full rounded-lg border border-border/40 bg-base shadow-lg">
                    <div className="border-b border-border/30 p-2">
                      <input type="text" value={modelFilter} onChange={e => setModelFilter(e.target.value)}
                        placeholder="搜索模型..." autoFocus
                        className="h-7 w-full rounded-md bg-base px-2 text-xs ring-1 ring-border/30 focus:outline-none focus:ring-accent/40" />
                    </div>
                    <div className="max-h-56 overflow-y-auto py-1">
                      {modelsLoading ? (
                        <div className="flex items-center justify-center gap-1.5 py-6 text-xs text-muted">
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />加载中...
                        </div>
                      ) : modelsError ? (
                        <div className="px-3 py-4 text-center text-xs text-muted">获取失败: {modelsError}</div>
                      ) : filteredModelOptions.length === 0 ? (
                        <div className="px-3 py-4 text-center text-xs text-muted">{modelOptions.length ? '无匹配模型' : '未获取到模型'}</div>
                      ) : filteredModelOptions.map(id => (
                        <button key={id} type="button" onClick={() => { handleModelChange(id); setModelsOpen(false) }}
                          className={`block w-full truncate px-3 py-1.5 text-left font-mono text-xs transition-colors hover:bg-accent/10 ${model === id ? 'text-accent' : 'text-secondary'}`}>
                          {id}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </Field>
          </div>

          <Field label="API Key" hint="本站 Sub2API 的 API Key；计费走你自己的额度">
            <div className="flex gap-2">
              <div className="flex-1 relative">
                <input type={showKey ? 'text' : 'password'} value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configured ? `${s?.ai_api_key_masked} · 留空不修改` : 'sk-...'} className={`${INPUT_CLS} pr-9`} />
                <button onClick={() => setShowKey(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted/40 hover:text-muted" tabIndex={-1} aria-label={showKey ? '隐藏' : '显示'}>
                  {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              </div>
              <button onClick={handleTest} disabled={testing || !apiKey} className="h-9 px-3 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 disabled:opacity-40 transition-all flex items-center gap-1.5 shrink-0">
                {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
                测试
              </button>
            </div>
          </Field>

          <div className="border-t border-border/20" />

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Field label="自定义 User-Agent" inline>
                <Toggle checked={customUa} onChange={() => setCustomUa(v => !v)} />
              </Field>
            </div>
            {customUa && (
              <div className="flex gap-2">
                <input type="text" value={userAgent} onChange={e => setUserAgent(e.target.value)} placeholder="粘贴浏览器 User-Agent" className={`${INPUT_CLS} flex-1`} />
                <button type="button" onClick={genRandomUa} title="随机生成浏览器 User-Agent" className="h-9 px-2.5 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 transition-all flex items-center gap-1.5 shrink-0">
                  <Shuffle className="h-3 w-3" /> 随机
                </button>
              </div>
            )}
          </div>

          <div className="border-t border-border/20 pt-4">
            <div className="grid grid-cols-2 gap-4">
              <Field label="输出上限 max_tokens" hint="所有 AI 任务的输出 token 上限, 任务请求会被钳制到此值; 默认 16384">
                <input type="number" min={1} value={maxOutputTokens} onChange={e => setMaxOutputTokens(e.target.value)} placeholder="16384" className={INPUT_CLS} />
              </Field>
              <Field label="上下文窗口 (输入上限)" hint="输入估算超出此窗口时会报错并提示调大; 默认 128000">
                <input type="number" min={1} value={contextWindow} onChange={e => setContextWindow(e.target.value)} placeholder="128000" className={INPUT_CLS} />
              </Field>
            </div>
          </div>
        </div>
      </Card>

      <div className="rounded-card border border-amber-400/20 bg-amber-400/[0.04] px-4 py-3 flex items-start gap-3">
        <Shield className="h-4 w-4 text-amber-400/70 mt-0.5 shrink-0" />
        <div className="text-[11px] text-amber-400/70 leading-relaxed">
          API 地址固定为本站 AI 网关，不可修改。API Key 仅保存在本机项目文件中, 不会上传到任何服务器。请妥善保管。
        </div>
      </div>

      <div className="flex gap-2">
        <button onClick={() => save.mutate()} disabled={save.isPending || !canSave} className="flex-1 h-10 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all">
          {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
          {save.isPending ? '保存中...' : saved ? '已保存' : '保存配置'}
        </button>
        {configured && (
          <button onClick={() => setConfirmClear(true)} disabled={clear.isPending} className="h-10 px-4 rounded-xl bg-elevated text-secondary hover:text-danger text-sm flex items-center justify-center gap-1.5 hover:bg-elevated/80 disabled:opacity-50 transition-all shrink-0" title="Clear AI provider configuration">
            <Trash2 className="h-4 w-4" />
            清空
          </button>
        )}
      </div>

      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setConfirmClear(false)} />
          <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
            <h3 className="text-sm font-medium text-foreground mb-2">清空 AI 配置</h3>
            <p className="text-xs text-secondary mb-5 leading-relaxed">
              这会清空已保存的 API Key、模型和 User-Agent。API 地址是固定的，不受影响。之后可以重新配置。
            </p>
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => setConfirmClear(false)} className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors">
                取消
              </button>
              <button onClick={() => clear.mutate()} disabled={clear.isPending} className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50">
                {clear.isPending ? '清空中...' : '确认'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
    </HighlightContext.Provider>
  )
}

// ===== 通用卡片(与 Keys 页风格统一) =====

// 卡片定位锚点: highlight=<anchor> 时滚动到视口中央并闪烁 (见 useCardFlash)
const HighlightContext = createContext('')

interface CardProps {
  icon: React.ComponentType<{ className?: string }>
  title: string
  right?: React.ReactNode
  children: React.ReactNode
  anchor?: string
}

function Card({ icon: Icon, title, right, children, anchor }: CardProps) {
  const highlight = useContext(HighlightContext)
  const { ref, flash } = useCardFlash(anchor ? highlight : undefined, anchor ?? '')
  const inner = (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
        </div>
        {right}
      </div>
      {children}
    </section>
  )
  if (!anchor) return inner
  return (
    <div ref={ref} id={anchor} className={cardFlashCls(flash)}>
      {inner}
    </div>
  )
}

// ===== 表单字段(统一 label + 输入框样式) =====

function Field({ label, hint, inline, children }: {
  label: string
  hint?: string
  inline?: boolean
  children: React.ReactNode
}) {
  if (inline) {
    return (
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
          {hint && <div className="text-[10px] text-muted mt-0.5">{hint}</div>}
        </div>
        {children}
      </div>
    )
  }
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

// ===== 开关 =====

function Toggle({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${checked ? 'bg-accent' : 'bg-elevated'}`}
      aria-pressed={checked}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${checked ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  )
}
