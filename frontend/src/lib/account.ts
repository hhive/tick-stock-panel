/**
 * 多用户账号 — Sub2API 跳转凭证的地址栏清理与内存持有; 跨账号本地残留清理。
 *
 * 安全姿态:
 *   - ``apikey`` 是 bearer 凭证, 只随 URL 传入一次。读取后**立刻**用
 *     ``history.replaceState`` 从地址栏抹掉 —— 既避免留在浏览器历史, 也避免
 *     相对外链把它带进 Referer。
 *   - 凭证只存在模块级变量里(内存), 绝不写 localStorage / sessionStorage / cookie。
 *     刷新即失效是预期行为(刷新后地址栏里也没有参数了), 换来的是落盘泄漏面为零。
 *   - 登出/换账号时必须调 ``clearAccountScopedStorage()``(见下), 否则同一浏览器上
 *     下一个账号能读到上一个账号落盘的内容 (2026-09-27 复核 B3)。
 */

/** 跳转凭证的查询参数名 (由 Sub2API 站点拼在跳转链接上) */
export const JUMP_KEY_PARAM = 'apikey'

/**
 * Sub2API 站点地址 — 登录页「一键进入」按钮的落地页。
 * 与后端 ``app.services.sub2api_verify.SUB2API_BASE_URL`` 的默认值保持一致;
 * 换环境时用构建期变量 ``VITE_SUB2API_SITE_URL`` 覆盖。
 */
export const SUB2API_SITE_URL: string =
  import.meta.env.VITE_SUB2API_SITE_URL || 'https://xiaoni-model.top'

/** 内存中的待绑定凭证 (仅本次页面生命周期有效) */
let _heldKey: string | null = null

/**
 * 从地址栏读取 apikey 并同步抹掉, 把 key 记到内存。
 * 无凭证时返回 null (此时不动地址栏)。
 */
export function captureJumpKeyFromUrl(): string | null {
  const url = new URL(window.location.href)
  const key = url.searchParams.get(JUMP_KEY_PARAM)
  if (!key) return null
  url.searchParams.delete(JUMP_KEY_PARAM)
  // 只去掉凭证本身, 保留 path / 其它查询参数 / hash
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  _heldKey = key
  return key
}

/** 当前待绑定凭证 (null = 无)。 */
export function getHeldJumpKey(): string | null {
  return _heldKey
}

export function setHeldJumpKey(key: string | null): void {
  _heldKey = key
}

/** 仅测试用: 复位模块级状态 (生产代码不要调用)。 */
export function resetAccountStateForTests(): void {
  _heldKey = null
  // 身份复位到哨兵**并通知监听者** (不是静默改值): 否则会留下「account 说哨兵、按身份
  // 隔离的内存单例还停在上一个身份」的错位, 测试之间就不再是确定性的。
  _identity = ANONYMOUS_IDENTITY
  notifyIdentityListeners(ANONYMOUS_IDENTITY)
}

// ===== 当前身份 (按身份隔离本地数据的依据) =====

/**
 * 未登录 / 游客 / 旧单密码会话共用的哨兵身份。
 *
 * 旧单密码通道没有 email (`authStatus` 回 email:null), 它就是一个人的自托管面板,
 * 与游客共用同一命名空间是可接受的; 有 email 的账号各用各的。
 */
export const ANONYMOUS_IDENTITY = '__anonymous__'

let _identity: string = ANONYMOUS_IDENTITY

/** 归一化身份: email 去空白转小写, 空则哨兵 (大小写不同的同一账号不能分成两份数据)。 */
export function normalizeIdentity(email?: string | null): string {
  const value = (email ?? '').trim().toLowerCase()
  return value || ANONYMOUS_IDENTITY
}

/** 当前身份 (按身份隔离本地数据用, 见 assistant store 的命名空间)。 */
export function currentAccountIdentity(): string {
  return _identity
}

const identityListeners = new Set<(identity: string) => void>()

/**
 * 登记「身份变化」监听 —— 拥有按身份隔离的内存单例的模块在模块顶层登记一次,
 * 换身份时把自己的内存态重新装载成新身份的那份 (不是清空)。
 */
export function onAccountIdentityChange(listener: (identity: string) => void): void {
  identityListeners.add(listener)
}

/**
 * 应用当前身份 —— 登出、登录成功、以及**刷新后 authStatus 就位**时都要调
 * (刷新后 store 先按哨兵装载, 身份就位后切回自己那份, 否则本人会看到空历史)。
 *
 * 身份字符串与之前相同则直接返回: 重复拿到同一身份不能打断正在进行的生成/重新装载。
 */
export function applyAccountIdentity(email?: string | null): void {
  const next = normalizeIdentity(email)
  if (next === _identity) return
  _identity = next
  notifyIdentityListeners(next)
}

/** 通知所有身份监听者重新装载 (单个监听器出错不拖住其余)。 */
function notifyIdentityListeners(identity: string): void {
  for (const listener of identityListeners) {
    try {
      listener(identity)
    } catch (error) {
      console.warn('[account] 身份切换后的重新装载失败', error)
    }
  }
}

// ===== 跨账号本地残留清理 =====

/**
 * 账户私有 localStorage 键清单 —— 换账号时要清掉的**指针/断点**类键。
 *
 * 为什么集中登记: 这些键由各页面/模块各自写入, 登出时只清一部分 = 上一个账号的
 * 内容继续留给下一个账号看 (2026-09-27 复核 B3)。**新增账户私有键时必须登记到这里**,
 * 并在对应模块里注册内存态复位 (见 registerAccountScopedReset), 否则登出就是漏的。
 *
 * 两类键**不要**登记:
 *   - 设备级偏好 —— 主题 / 导航态 / 列宽 / 气泡位置 / 告警提示音 / 引导语「不再提示」
 *     等, 它们属于这台设备而不是某个账号, 跨账号保留是合理的;
 *   - **用户内容**(如助手会话历史) —— 清单里的键是"清掉只是重新点一次"的指针/断点,
 *     而内容清掉就是**销毁用户自己的东西**。用户内容一律改按身份命名空间隔离
 *     (`<键>.<身份>`, 见 normalizeIdentity / applyAccountIdentity), 登出只切命名空间。
 */
export const ACCOUNT_SCOPED_STORAGE_KEYS: readonly string[] = [
  // 当前选中的模拟盘账户 id —— 指向上一账号的账户 (pages/Paper.tsx)
  'paper.account',
  // 因子挖掘工作台草稿: 标的 / 因子 / 参数 (pages/backtest/MiningWorkbench.tsx)
  'mining_workbench_draft_v1',
  // 回测 / 因子优化 / 滚动优化的断点续连句柄: 内含上一账号的策略参数与 job key
  // (lib/backtestTask.ts, lib/optimizerTask.ts, lib/walkforwardTask.ts, lib/miningTask.ts)
  'backtest_reconnect',
  'optimizer_reconnect',
  'optimizer_job_key',
  'walkforward_reconnect',
  'walkforward_job_key',
  'mining_active_run_id',
]

/**
 * 内存态复位回调 — 只清 localStorage 不够: 本项目多个 store 是**模块级单例**
 * (助手会话 / 任务状态), 登出走的是客户端路由, 模块不会重新加载, 内存里的
 * 上一账号数据会原封不动带到下一个账号。拥有这类状态的模块在模块顶层调本函数
 * 登记一次, 清理时随之一并复位。
 */
const memoryResetters = new Set<() => void>()

/** 登记「本地数据被清理时一并复位内存态」的回调 (模块顶层调用一次即可)。 */
export function registerAccountScopedReset(reset: () => void): void {
  memoryResetters.add(reset)
}

/**
 * 清掉账户私有的本地数据 —— 登出与**登录成功**都要调:
 * 登出路径好理解; 登录路径同样必要, 因为会话过期后换人登录不经过 signOut。
 *
 * 只清 ACCOUNT_SCOPED_STORAGE_KEYS 里的键 (不是 localStorage.clear()):
 * 设备级偏好必须留下。
 */
export function clearAccountScopedStorage(): void {
  for (const key of ACCOUNT_SCOPED_STORAGE_KEYS) {
    try {
      window.localStorage.removeItem(key)
    } catch { /* 隐私模式/禁用存储: 没有可清的东西 */ }
  }
  for (const reset of memoryResetters) {
    try {
      reset()
    } catch (error) {
      // 单个模块复位失败不能拖住其余清理 (登出必须总能落到未登录态)
      console.warn('[account] 账户数据复位失败', error)
    }
  }
}
