import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import SettingsLayout from '../src/views/settings/SettingsLayout.vue'
import App from '../src/App.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const CONFIG = {
  path: 'config/ccat_2026.json',
  config: {
    season: '2026',
    excel_file: './output/ccat_2026.xlsx',
    tables: {
      sponsor: {
        columns: {
          email: { type: 'email' },
        },
      },
    },
    email_check: { enabled: true, delay: 0.25 },
    vocabularies: { sponsor_affiliation: ['Individual', 'Organization'] },
  },
}

function asRole(roles: string[]): void {
  useSessionStore().me = {
    username: roles.includes('admin') ? 'cmendres' : 'lmboswell',
    roles,
    csrf_token: 'csrf',
  }
}

/** Mount the settings pages at one section. */
async function mountSettings(path: string) {
  const { wrapper, router } = await mountRouted(SettingsLayout, {
    path,
    setup: () => asRole(['admin']),
  })
  await flushPromises()
  return { wrapper, router }
}

/** The body of the last recorded PUT of the configuration tree. */
function savedBody(fetchMock: ReturnType<typeof mockRoutes>) {
  const puts = fetchMock.mock.calls.filter(
    (entry) =>
      String(entry[0]).endsWith('/api/v1/config') &&
      String((entry[1] as RequestInit).method) === 'PUT',
  )
  return puts.length > 0 ? bodyOf(puts[puts.length - 1]) : null
}

/** Config routes whose save echoes the stored tree back, as the API does. */
function echoConfigRoutes(): ReturnType<typeof mockRoutes> {
  return mockRoutes([
    (url, init) =>
      url.endsWith('/api/v1/config') && init.method === 'PUT'
        ? jsonResponse(200, {
            path: CONFIG.path,
            config: JSON.parse(String(init.body)),
          })
        : undefined,
    (url, init) =>
      url.endsWith('/api/v1/config') && init.method === 'GET'
        ? jsonResponse(200, CONFIG)
        : undefined,
  ])
}

beforeEach(() => setActivePinia(createPinia()))

describe('settings', () => {
  it('is role gated: non-admins see an explicit refusal and no request', async () => {
    const fetchMock = mockRoutes([])
    const { wrapper } = await mountRouted(SettingsLayout, {
      path: '/settings',
      setup: () => asRole(['auditor']),
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="settings-forbidden"]').exists()).toBe(
      true,
    )
    expect(fetchMock.mock.calls).toHaveLength(0)
  })

  it('hides the settings nav entry for non-admins', async () => {
    mockRoutes([])
    const { wrapper } = await mountRouted(App, {
      path: '/',
      setup: () => asRole(['auditor']),
    })
    await flushPromises()
    const links = wrapper.findAll('nav a')
    expect(links.some((link) => link.text().includes('Settings'))).toBe(false)
  })

  it('lists the sections as a clean index', async () => {
    mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'GET'
          ? jsonResponse(200, CONFIG)
          : undefined,
    ])
    const { wrapper } = await mountSettings('/settings')

    const links = wrapper.findAll('[data-testid="settings-section-link"]')
    const slugs = links.map((link) => link.attributes('data-slug'))
    expect(slugs).toContain('general')
    expect(slugs).toContain('vocabularies')
    expect(slugs).toContain('tables')
  })

  it('shows one section per subscreen', async () => {
    mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'GET'
          ? jsonResponse(200, CONFIG)
          : undefined,
    ])
    const { wrapper } = await mountSettings('/settings/vocabularies')

    const section = wrapper.get('[data-testid="settings-section"]')
    expect(section.attributes('data-slug')).toBe('vocabularies')
    expect(
      wrapper.find('input[name="vocabularies.sponsor_affiliation.0"]').exists(),
    ).toBe(true)
  })

  it('edits a value and saves the whole tree', async () => {
    const fetchMock = echoConfigRoutes()
    const { wrapper } = await mountSettings('/settings/general')

    const input = wrapper.get('input[name="season"]')
    expect((input.element as HTMLInputElement).value).toBe('2026')
    await input.setValue('2027')
    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({ season: '2027' })
  })

  it('adds and deletes list entries', async () => {
    const fetchMock = echoConfigRoutes()
    const { wrapper } = await mountSettings('/settings/vocabularies')

    await wrapper
      .get(
        '[data-testid="settings-add-item"][data-path="vocabularies.sponsor_affiliation"]',
      )
      .trigger('click')
    await flushPromises()

    const added = wrapper.get('input[name="vocabularies.sponsor_affiliation.2"]')
    await added.setValue('Walmart')
    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({
      vocabularies: {
        sponsor_affiliation: ['Individual', 'Organization', 'Walmart'],
      },
    })

    await wrapper
      .get(
        '[data-testid="settings-delete"][data-path="vocabularies.sponsor_affiliation.0"]',
      )
      .trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({
      vocabularies: {
        sponsor_affiliation: ['Organization', 'Walmart'],
      },
    })
  })

  it('adds a new vocabulary list and fills it', async () => {
    const fetchMock = echoConfigRoutes()
    const { wrapper } = await mountSettings('/settings/vocabularies')

    await wrapper
      .get('[data-testid="settings-add-entry"][data-path="vocabularies"]')
      .trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="settings-new-key"]').setValue('child_gender')
    await wrapper.get('[data-testid="settings-confirm-add"]').trigger('click')
    await flushPromises()

    await wrapper
      .get(
        '[data-testid="settings-add-item"][data-path="vocabularies.child_gender"]',
      )
      .trigger('click')
    await flushPromises()
    await wrapper.get('input[name="vocabularies.child_gender.0"]').setValue('Male')

    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({
      vocabularies: { child_gender: ['Male'] },
    })
  })

  it('adds and deletes table columns', async () => {
    const fetchMock = echoConfigRoutes()
    const { wrapper } = await mountSettings('/settings/tables')

    expect(
      wrapper.find('[data-testid="settings-add-entry"][data-path="tables"]')
        .exists(),
    ).toBe(true)

    await wrapper
      .get(
        '[data-testid="settings-add-entry"][data-path="tables.sponsor.columns"]',
      )
      .trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="settings-new-key"]').setValue('last_name')
    await wrapper.get('[data-testid="settings-confirm-add"]').trigger('click')
    await flushPromises()

    const added = wrapper.get(
      'input[name="tables.sponsor.columns.last_name.type"]',
    )
    expect((added.element as HTMLInputElement).value).toBe('string')

    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({
      tables: {
        sponsor: {
          columns: { last_name: { type: 'string' } },
        },
      },
    })

    await wrapper
      .get(
        '[data-testid="settings-delete"][data-path="tables.sponsor.columns.last_name"]',
      )
      .trigger('click')
    await flushPromises()

    expect(
      wrapper.find('input[name="tables.sponsor.columns.last_name.type"]').exists(),
    ).toBe(false)

    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    expect(savedBody(fetchMock)).toMatchObject({
      tables: { sponsor: { columns: { email: { type: 'email' } } } },
    })
    expect(savedBody(fetchMock)).not.toHaveProperty(
      'tables.sponsor.columns.last_name',
    )
  })

  it('surfaces 422 validation errors by path', async () => {
    mockRoutes([
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'PUT'
          ? jsonResponse(422, {
              detail: {
                code: 'invalid_config',
                message: 'Configuration is invalid',
                errors: [
                  {
                    path: 'api.port',
                    message: 'Input should be a valid integer',
                  },
                ],
              },
            })
          : undefined,
      (url, init) =>
        url.endsWith('/api/v1/config') && init.method === 'GET'
          ? jsonResponse(200, CONFIG)
          : undefined,
    ])
    const { wrapper } = await mountSettings('/settings/general')

    await wrapper.get('input[name="season"]').setValue('2027')
    await wrapper.get('[data-testid="settings-save"]').trigger('click')
    await flushPromises()

    const error = wrapper.get('[data-testid="settings-error"]')
    expect(error.text()).toContain('api.port')
    expect(error.text()).toContain('Input should be a valid integer')
  })
})
