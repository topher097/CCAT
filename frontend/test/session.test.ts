import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { configureClient } from '../src/api/client'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes } from './helpers'

const ME = {
  username: 'cmendres',
  display_name: 'C',
  roles: ['auditor', 'coordinator'],
  csrf_token: 'csrf-123',
}
const SEASON = { id: 1, name: '2026', xmas_date: '2026-12-25' }

beforeEach(() => {
  setActivePinia(createPinia())
  configureClient({ csrfToken: () => null, onUnauthorized: () => {} })
})

describe('session store', () => {
  it('logs in, loads me and the active season, keeps state in memory', async () => {
    mockRoutes([
      (url) =>
        url.includes('/auth/login')
          ? jsonResponse(204, null)
          : undefined,
      (url) => (url.includes('/auth/me') ? jsonResponse(200, ME) : undefined),
      (url) =>
        url.includes('/seasons/current') ? jsonResponse(200, SEASON) : undefined,
    ])
    const session = useSessionStore()

    await expect(session.login('cmendres')).resolves.toBe(true)

    expect(session.authenticated).toBe(true)
    expect(session.username).toBe('cmendres')
    expect(session.roles).toEqual(['auditor', 'coordinator'])
    expect(session.csrfToken).toBe('csrf-123')
    expect(session.season).toEqual(SEASON)
    expect(session.hasRole('admin')).toBe(false)
    expect(session.hasRole('auditor')).toBe(true)
    // Never persisted (§7.2).
    expect(localStorage.length).toBe(0)
  })

  it('maps a 401 login to the unknown-user state', async () => {
    mockRoutes([
      (url) =>
        url.includes('/auth/login')
          ? jsonResponse(401, {
              detail: { code: 'unauthenticated', message: 'unknown user' },
            })
          : undefined,
    ])
    const session = useSessionStore()

    await expect(session.login('nobody')).resolves.toBe(false)
    expect(session.loginError).toEqual({
      status: 401,
      message: 'Unknown or inactive user.',
    })
    expect(session.authenticated).toBe(false)
  })

  it('exposes 403 (non-dev mode) and 429 (rate limit) states', async () => {
    mockRoutes([
      (url) =>
        url.includes('/auth/login')
          ? jsonResponse(403, {
              detail: { code: 'auth_mode', message: 'dev only' },
            })
          : undefined,
    ])
    const session = useSessionStore()
    await session.login('cmendres')
    expect(session.loginError?.status).toBe(403)
    expect(session.loginError?.message).toContain('development')

    mockRoutes([
      (url) =>
        url.includes('/auth/login')
          ? jsonResponse(429, {
              detail: { code: 'rate_limited', message: 'slow down' },
            })
          : undefined,
    ])
    await session.login('cmendres')
    expect(session.loginError?.status).toBe(429)
    expect(session.loginError?.message).toContain('Too many')
  })

  it('restore returns false and clears identity without a session', async () => {
    mockRoutes([
      () =>
        jsonResponse(401, {
          detail: { code: 'unauthenticated', message: 'required' },
        }),
    ])
    const session = useSessionStore()
    await expect(session.restore()).resolves.toBe(false)
    expect(session.me).toBeNull()
    expect(session.season).toBeNull()
  })

  it('logout revokes and clears all cached identity', async () => {
    mockRoutes([
      (url) => (url.includes('/auth/me') ? jsonResponse(200, ME) : undefined),
      (url) =>
        url.includes('/seasons/current') ? jsonResponse(200, SEASON) : undefined,
      (url) =>
        url.includes('/auth/logout') ? jsonResponse(204, null) : undefined,
    ])
    const session = useSessionStore()
    await session.restore()
    expect(session.authenticated).toBe(true)

    await session.logout()
    expect(session.me).toBeNull()
    expect(session.season).toBeNull()
    expect(session.roles).toEqual([])
  })

  it('clears local state even when the logout request fails', async () => {
    mockRoutes([
      (url) => (url.includes('/auth/me') ? jsonResponse(200, ME) : undefined),
      (url) =>
        url.includes('/seasons/current') ? jsonResponse(200, SEASON) : undefined,
      (url) => {
        if (url.includes('/auth/logout')) {
          throw new TypeError('offline')
        }
        return undefined
      },
    ])
    const session = useSessionStore()
    await session.restore()
    await session.logout()
    expect(session.me).toBeNull()
  })
})
