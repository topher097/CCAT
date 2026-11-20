import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import DatabaseView from '../src/views/DatabaseView.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const TABLES = [
  { name: 'final_child', columns: ['id', 'first_name'], row_count: 128 },
  { name: 'sponsor', columns: ['id'], row_count: 3 },
]

const SELECT_RESULT = {
  columns: ['id', 'first_name'],
  rows: [{ id: 1, first_name: 'Ana' }],
  row_count: 1,
  truncated: false,
  affected_rows: 0,
  duration_ms: 2,
}

function asRole(roles: string[]): void {
  useSessionStore().me = {
    username: roles.includes('admin') ? 'cmendres' : 'lmboswell',
    roles,
    csrf_token: 'csrf',
  }
}

function dbRoutes(result: unknown = SELECT_RESULT) {
  return mockRoutes([
    (url) =>
      url.endsWith('/api/v1/admin/db/tables')
        ? jsonResponse(200, TABLES)
        : undefined,
    (url, init) =>
      url.endsWith('/api/v1/admin/db/query') && String(init.method) === 'POST'
        ? jsonResponse(200, result)
        : undefined,
  ])
}

function queryCalls(fetchMock: ReturnType<typeof mockRoutes>) {
  return fetchMock.mock.calls.filter((entry) =>
    String(entry[0]).endsWith('/api/v1/admin/db/query'),
  )
}

beforeEach(() => setActivePinia(createPinia()))

describe('database console', () => {
  it('is role gated: non-admins see a refusal and make no request', async () => {
    const fetchMock = mockRoutes([])
    const { wrapper } = await mountRouted(DatabaseView, {
      path: '/admin',
      setup: () => asRole(['auditor']),
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="db-forbidden"]').exists()).toBe(true)
    expect(fetchMock.mock.calls).toHaveLength(0)
  })

  it('lists tables and renders the rows of a SELECT', async () => {
    dbRoutes()
    const { wrapper } = await mountRouted(DatabaseView, {
      path: '/admin',
      setup: () => asRole(['admin']),
    })
    await flushPromises()

    expect(wrapper.text()).toContain('final_child (128)')

    await wrapper.get('[data-testid="db-sql"]').setValue(
      'SELECT * FROM "final_child" LIMIT 200',
    )
    await wrapper.get('[data-testid="db-run"]').trigger('click')
    await flushPromises()

    const table = wrapper.get('[data-testid="db-results"]')
    expect(table.text()).toContain('first_name')
    expect(table.text()).toContain('Ana')
  })

  it('sends write: false by default and write: true when the toggle is set', async () => {
    const fetchMock = dbRoutes()
    const { wrapper } = await mountRouted(DatabaseView, {
      path: '/admin',
      setup: () => asRole(['admin']),
    })
    await flushPromises()

    await wrapper.get('[data-testid="db-sql"]').setValue('DELETE FROM sponsor')
    await wrapper.get('[data-testid="db-run"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="db-write"]').setValue(true)
    await wrapper.get('[data-testid="db-run"]').trigger('click')
    await flushPromises()

    const calls = queryCalls(fetchMock)
    expect(bodyOf(calls[0]!)).toEqual({
      sql: 'DELETE FROM sponsor',
      write: false,
    })
    expect(bodyOf(calls[1]!)).toEqual({
      sql: 'DELETE FROM sponsor',
      write: true,
    })
  })
})
