import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import AdminView from '../src/views/AdminView.vue'
import App from '../src/App.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const USERS = [
  {
    id: 2,
    username: 'lmboswell',
    display_name: 'L',
    active: true,
    roles: ['auditor', 'coordinator'],
    dev_no_password: true,
  },
]

function asRole(roles: string[]): void {
  useSessionStore().me = {
    username: roles.includes('admin') ? 'cmendres' : 'lmboswell',
    roles,
    csrf_token: 'csrf',
  }
}

beforeEach(() => setActivePinia(createPinia()))

describe('admin users', () => {
  it('is role gated: non-admins see an explicit refusal and no request', async () => {
    const fetchMock = mockRoutes([])
    const { wrapper } = await mountRouted(AdminView, {
      path: '/admin',
      setup: () => asRole(['auditor']),
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="admin-forbidden"]').exists()).toBe(true)
    expect(fetchMock.mock.calls).toHaveLength(0)
  })

  it('hides the admin nav entry for non-admins', async () => {
    mockRoutes([])
    const { wrapper } = await mountRouted(App, {
      path: '/',
      setup: () => asRole(['auditor']),
    })
    await flushPromises()
    const links = wrapper.findAll('nav a')
    expect(links.some((link) => link.text().includes('Admin'))).toBe(false)
  })

  it('lists users and creates a new account', async () => {
    const fresh = {
      id: 3,
      username: 'newbie',
      display_name: 'New',
      active: true,
      roles: ['auditor'],
      dev_no_password: false,
    }
    const fetchMock = mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/users') && String(init.method) === 'POST'
          ? jsonResponse(201, fresh)
          : undefined,
      (url) =>
        url.endsWith('/api/v1/users') ? jsonResponse(200, USERS) : undefined,
    ])
    const { wrapper } = await mountRouted(AdminView, {
      path: '/admin',
      setup: () => asRole(['admin']),
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="admin-users"]').text()).toContain(
      'lmboswell',
    )

    const form = wrapper.get('form')
    await form.get('input[name="username"]').setValue('newbie')
    await form.get('input[name="display_name"]').setValue('New')
    await form.get('input[value="auditor"]').setValue(true)
    await form.trigger('submit')
    await flushPromises()

    const call = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/api/v1/users') &&
        String((entry[1] as RequestInit).method) === 'POST',
    )
    expect(bodyOf(call!)).toEqual({
      username: 'newbie',
      display_name: 'New',
      roles: ['auditor'],
      active: true,
    })
    expect(wrapper.text()).toContain('Created newbie.')
  })

  it('patches roles and active state for an existing account', async () => {
    const updated = {
      ...USERS[0],
      active: false,
      roles: ['auditor', 'coordinator', 'admin'],
    }
    const fetchMock = mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/users/2') && String(init.method) === 'PATCH'
          ? jsonResponse(200, updated)
          : undefined,
      (url) =>
        url.endsWith('/api/v1/users') ? jsonResponse(200, USERS) : undefined,
    ])
    const { wrapper } = await mountRouted(AdminView, {
      path: '/admin',
      setup: () => asRole(['admin']),
    })
    await flushPromises()

    const row = wrapper.get('[data-testid="admin-users"] tbody tr')
    await row.find('input[value="admin"]').setValue(true)
    const active = row.findAll('input[type="checkbox"]').at(-1)
    await active!.setValue(false)
    await row.get('button').trigger('click')
    await flushPromises()

    const call = fetchMock.mock.calls.find((entry) =>
      String(entry[0]).endsWith('/api/v1/users/2'),
    )
    expect(bodyOf(call!)).toEqual({
      display_name: 'L',
      active: false,
      roles: ['auditor', 'coordinator', 'admin'],
    })
  })
})
