/** Authenticated session state — memory only, never persisted (§7.2). */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError, api } from '../api/client'
import type { Schemas } from '../api/client'

export type LoginFailure = { status: number; message: string }

const LOGIN_MESSAGES: Record<number, string> = {
  401: 'Unknown or inactive user.',
  403: 'Passwordless login is only available in development mode.',
  429: 'Too many login attempts; wait a minute and retry.',
  0: 'Cannot reach the server.',
}

/** Map one login failure to a user-facing message. */
function loginMessage(error: unknown): LoginFailure {
  if (error instanceof ApiError) {
    return {
      status: error.status,
      message: LOGIN_MESSAGES[error.status] ?? error.message,
    }
  }
  return { status: 0, message: LOGIN_MESSAGES[0] ?? 'Login failed.' }
}

export const useSessionStore = defineStore('session', () => {
  const me = ref<Schemas['MeView'] | null>(null)
  const season = ref<Schemas['SeasonView'] | null>(null)
  const loading = ref(false)
  const loginError = ref<LoginFailure | null>(null)

  const authenticated = computed(() => me.value !== null)
  const username = computed(() => me.value?.username ?? null)
  const roles = computed(() => me.value?.roles ?? [])
  const csrfToken = computed(() => me.value?.csrf_token ?? null)

  function hasRole(...names: string[]): boolean {
    return names.some((name) => roles.value.includes(name))
  }

  /** Drop all in-memory identity/season state (logout/season switch). */
  function clear(): void {
    me.value = null
    season.value = null
    loginError.value = null
  }

  async function loadSeason(): Promise<void> {
    try {
      season.value = await api.get<Schemas['SeasonView']>(
        '/api/v1/seasons/current',
      )
    } catch {
      season.value = null
    }
  }

  /** Restore the session from the cookie; true when authenticated. */
  async function restore(): Promise<boolean> {
    loading.value = true
    try {
      me.value = await api.get<Schemas['MeView']>('/api/v1/auth/me')
      await loadSeason()
      return true
    } catch {
      me.value = null
      return false
    } finally {
      loading.value = false
    }
  }

  /** Dev username-only login; failure is reported, not thrown. */
  async function login(usernameInput: string): Promise<boolean> {
    loginError.value = null
    loading.value = true
    try {
      await api.post('/api/v1/auth/login', {
        username: usernameInput.trim(),
      })
      return await restore()
    } catch (error) {
      const failure = loginMessage(error)
      loginError.value = failure
      return false
    } finally {
      loading.value = false
    }
  }

  /** Revoke the session server-side, then clear local state. */
  async function logout(): Promise<void> {
    try {
      await api.post('/api/v1/auth/logout')
    } catch {
      // A dead session is already logged out; clear locally regardless.
    } finally {
      clear()
    }
  }

  return {
    me,
    season,
    loading,
    loginError,
    authenticated,
    username,
    roles,
    csrfToken,
    hasRole,
    clear,
    loadSeason,
    restore,
    login,
    logout,
  }
})
