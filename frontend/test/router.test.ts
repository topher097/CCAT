import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  configureClient: vi.fn(),
}))

vi.mock('../src/api/client', () => {
  class ApiError extends Error {
    status: number
    code: string
    context: Record<string, unknown>
    constructor(
      status: number,
      code: string,
      message: string,
      context: Record<string, unknown> = {},
    ) {
      super(message)
      this.status = status
      this.code = code
      this.context = context
    }
  }
  return {
    api: { get: mocks.get, post: mocks.post, patch: mocks.patch },
    configureClient: mocks.configureClient,
    ApiError,
  }
})

import { createAppRouter, routes } from '../src/router'
import { formDirty, setDirty } from '../src/composables/useDirtyGuard'

const ME = {
  username: 'cmendres',
  roles: ['auditor'],
  csrf_token: 'csrf',
}
const SEASON = { id: 1, name: '2026', xmas_date: '2026-12-25' }

beforeEach(() => {
  setActivePinia(createPinia())
  mocks.get.mockReset()
  mocks.post.mockReset()
  mocks.patch.mockReset()
})

function authenticated() {
  mocks.get.mockImplementation((path: string) => {
    if (path.includes('/auth/me')) return Promise.resolve(ME)
    if (path.includes('/seasons/current')) return Promise.resolve(SEASON)
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

function unauthenticated() {
  mocks.get.mockImplementation((path: string) =>
    Promise.reject(new Error(`unauthenticated ${path}`)),
  )
}

describe('router map', () => {
  it('contains every §7 area and audit sub-route', () => {
    const paths = routes.map((route) => route.path)
    expect(paths).toEqual(
      expect.arrayContaining([
        '/',
        '/login',
        '/acquire',
        '/screen',
        '/audit',
        '/audit/sponsors',
        '/audit/sponsors/:id',
        '/audit/families',
        '/audit/families/:id',
        '/audit/families/:familyId/children/:childId',
        '/audit/children',
        '/audit/children/:id',
        '/match',
        '/generate',
        '/stats',
        '/clarify',
        '/clarify/:familyId',
        '/jobs',
        '/jobs/:id',
        '/admin',
      ]),
    )
  })

  it('redirects the old manual matching address to the match page', async () => {
    authenticated()
    const router = createAppRouter()
    await router.push('/match/manual?sponsor_id=42')
    expect(router.currentRoute.value.path).toBe('/match')
    expect(router.currentRoute.value.query.sponsor_id).toBe('42')
  })

  it('redirects unauthenticated users to /login with a redirect', async () => {
    unauthenticated()
    const router = createAppRouter()
    await router.push('/audit/sponsors')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query.redirect).toBe('/audit/sponsors')
  })

  it('allows /login without a session', async () => {
    unauthenticated()
    const router = createAppRouter()
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('bounces an authenticated user away from /login', async () => {
    authenticated()
    const router = createAppRouter()
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('keeps an authenticated user on the requested route', async () => {
    authenticated()
    const router = createAppRouter()
    await router.push('/audit/children')
    expect(router.currentRoute.value.path).toBe('/audit/children')
  })

  it('redirects non-admins away from /admin', async () => {
    authenticated()
    const router = createAppRouter()
    await router.push('/admin')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('blocks navigation while a form is dirty unless confirmed', async () => {
    authenticated()
    const router = createAppRouter()
    await router.push('/audit/sponsors')
    setDirty(true)

    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)
    await router.push('/audit/families')
    expect(router.currentRoute.value.path).toBe('/audit/sponsors')
    expect(confirm).toHaveBeenCalledOnce()
    expect(formDirty.value).toBe(true)

    confirm.mockReturnValue(true)
    await router.push('/audit/families')
    expect(router.currentRoute.value.path).toBe('/audit/families')
    expect(formDirty.value).toBe(false)
  })
})
