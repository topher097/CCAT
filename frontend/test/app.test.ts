import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import App from '../src/App.vue'
import { useSessionStore } from '../src/stores/session'
import { mockRoutes, mountRouted } from './helpers'

function withSession(): void {
  const session = useSessionStore()
  session.season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
  session.me = {
    username: 'coordinator',
    roles: ['coordinator'],
    csrf_token: 'csrf',
  }
}

beforeEach(() => setActivePinia(createPinia()))

describe('app shell', () => {
  it('shows the section navigation on a normal page', async () => {
    mockRoutes([])
    const { wrapper } = await mountRouted(App, {
      path: '/',
      setup: withSession,
    })
    await flushPromises()

    expect(wrapper.find('nav.app-nav').exists()).toBe(true)
    expect(wrapper.get('nav.app-nav').text()).toContain('Dispatch')
    // Acquire and Screen moved onto the Registry page (§ role-limited).
    expect(wrapper.get('nav.app-nav').text()).not.toContain('Acquire')
    expect(wrapper.get('nav.app-nav').text()).not.toContain('Screen')
    expect(wrapper.find('header.app-header').exists()).toBe(true)
    expect(wrapper.get('[data-testid="header-user"]').text()).toBe('coordinator')
  })

  it('drops the section navigation in an embedded view', async () => {
    mockRoutes([])
    const { wrapper } = await mountRouted(App, {
      path: '/jobs/7?embed=1',
      setup: withSession,
    })
    await flushPromises()

    expect(wrapper.find('nav.app-nav').exists()).toBe(false)
    expect(wrapper.find('header.app-header').exists()).toBe(false)
    expect(wrapper.find('[data-testid="logout"]').exists()).toBe(false)
  })
})
