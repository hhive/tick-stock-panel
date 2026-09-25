/**
 * 多用户账号 — Sub2API 跳转凭证的地址栏清理与内存持有。
 *
 * 安全姿态:
 *   - ``apikey`` 是 bearer 凭证, 只随 URL 传入一次。读取后**立刻**用
 *     ``history.replaceState`` 从地址栏抹掉 —— 既避免留在浏览器历史, 也避免
 *     相对外链把它带进 Referer。
 *   - 凭证只存在模块级变量里(内存), 绝不写 localStorage / sessionStorage / cookie。
 *     刷新即失效是预期行为(刷新后地址栏里也没有参数了), 换来的是落盘泄漏面为零。
 */

/** 跳转凭证的查询参数名 (由 Sub2API 站点拼在跳转链接上) */
export const JUMP_KEY_PARAM = 'apikey'

/**
 * Sub2API 站点地址 — 登录页「一键进入」按钮的落地页。
 * 与后端 ``app.services.sub2api_verify.SUB2API_BASE_URL`` 的默认值保持一致;
 * 换环境时用构建期变量 ``VITE_SUB2API_SITE_URL`` 覆盖。
 */
export const SUB2API_SITE_URL: string =
  import.meta.env.VITE_SUB2API_SITE_URL || 'https://xiaoni-apikey.top'

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
}
