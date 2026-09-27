/**
 * 退出登录 —— 账号会话与单密码应急会话是**两套独立会话**, 按当前身份登出对应那一个。
 *
 * 为什么抽成公共 hook: 这个「按 mode 选端点」的分支很容易漏写, 而漏了的表现是
 * 「点了退出却还登录着」—— 一个不会报错、只会让人困惑的静默失败。侧边栏账号入口
 * 与设置页系统面板共用同一份, 就不会各写各的。
 *
 * 同时负责清掉账户私有的本地数据 (查询缓存 + 落盘 + 内存单例), 见 finally。
 */
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { api, type AuthStatus } from '@/lib/api'
import { applyAccountIdentity, clearAccountScopedStorage } from '@/lib/account'

export function useSignOut() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [signingOut, setSigningOut] = useState(false)

  const signOut = useCallback(async (mode?: AuthStatus['mode']) => {
    setSigningOut(true)
    try {
      // legacy = 单密码应急入口, account = 多用户账号; 两者会话表互相独立
      if (mode === 'legacy') await api.authLogout()
      else await api.accountLogout()
    } catch {
      // 登出接口失败(如会话已过期)也要让本地落到未登录态, 否则用户卡在已登录界面
    } finally {
      // 清查询缓存 + 账户私有**指针**类本地数据 (模拟盘账户 id / 任务续连句柄...):
      // 换账号后不能还看到上一个账号的数据。见 lib/account.ts 的清单。
      qc.clear()
      clearAccountScopedStorage()
      // 身份切到哨兵: 助手会话等**用户内容**按身份命名空间隔离, 这里只换窗口不删数据
      // (登出即删会把用户自己的东西也销毁 —— 那正是本次口径修正要避免的)。
      applyAccountIdentity(null)
      setSigningOut(false)
      navigate('/login', { replace: true })
    }
  }, [navigate, qc])

  return { signOut, signingOut }
}
