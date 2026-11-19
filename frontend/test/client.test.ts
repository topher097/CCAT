import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, configureClient } from '../src/api/client'
import { bodyOf, headersOf, jsonResponse, mockRoutes } from './helpers'

beforeEach(() => {
  configureClient({ csrfToken: () => 'csrf-token', onUnauthorized: () => {} })
})

describe('api client', () => {
  it('attaches the CSRF header to mutations only', async () => {
    const fetchMock = mockRoutes([() => jsonResponse(204, null)])
    await api.post('/api/v1/x', { value: 1 })
    await api.get('/api/v1/x')

    expect(headersOf(fetchMock.mock.calls[0]!)).toMatchObject({
      'X-CCAT-CSRF': 'csrf-token',
      'Content-Type': 'application/json',
    })
    expect(bodyOf(fetchMock.mock.calls[0]!)).toEqual({ value: 1 })
    expect(headersOf(fetchMock.mock.calls[1]!)).not.toHaveProperty(
      'X-CCAT-CSRF',
    )
    expect((fetchMock.mock.calls[1]![1] as RequestInit).credentials).toBe(
      'same-origin',
    )
  })

  it('omits CSRF when no session token exists', async () => {
    configureClient({ csrfToken: () => null })
    const fetchMock = mockRoutes([() => jsonResponse(204, null)])
    await api.post('/api/v1/auth/login', { username: 'x' })
    expect(headersOf(fetchMock.mock.calls[0]!)).not.toHaveProperty(
      'X-CCAT-CSRF',
    )
  })

  it('maps the server error body to ApiError with context', async () => {
    mockRoutes([
      () =>
        jsonResponse(412, {
          detail: {
            code: 'stale_version',
            message: 'row changed',
            entity: 'child',
            key: 'c1',
            current_version: 7,
            row: { first_name: 'Bob' },
          },
        }),
    ])

    const error = await api
      .patch('/api/v1/seasons/2026/children/c1', {}, { 'If-Match': '3' })
      .catch((failure: unknown) => failure)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(412)
    expect(apiError.code).toBe('stale_version')
    expect(apiError.message).toBe('row changed')
    expect(apiError.context.current_version).toBe(7)
    expect(apiError.context.row).toEqual({ first_name: 'Bob' })
  })

  it('calls the unauthorized hook on 401 and still throws', async () => {
    const onUnauthorized = vi.fn()
    configureClient({ onUnauthorized })
    mockRoutes([
      () =>
        jsonResponse(401, {
          detail: { code: 'unauthenticated', message: 'authentication required' },
        }),
    ])

    await expect(api.get('/api/v1/auth/me')).rejects.toMatchObject({
      status: 401,
      code: 'unauthenticated',
    })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('returns undefined for 204 responses', async () => {
    mockRoutes([() => jsonResponse(204, null)])
    await expect(api.post('/api/v1/auth/logout')).resolves.toBeUndefined()
  })

  it('reports network failures as status 0', async () => {
    mockRoutes([
      () => {
        throw new TypeError('connection refused')
      },
    ])
    await expect(api.get('/api/v1/healthz')).rejects.toMatchObject({
      status: 0,
      code: 'network_error',
    })
  })
})
