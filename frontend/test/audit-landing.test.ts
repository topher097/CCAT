import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import AuditLandingView from '../src/views/AuditLandingView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

const OVERVIEW = {
  season: '2026',
  entities: {
    child: {
      total: 12,
      by_status: {
        new: 5,
        in_progress: 2,
        needs_clarification: 1,
        ignored: 1,
        audited: 3,
      },
      ignored: 1,
      orphans: 2,
      raw_missing: 0,
      pending_ai: 4,
      generated: 1,
      assigned: 0,
    },
    family: {
      total: 3,
      by_status: { new: 2, audited: 1 },
      ignored: 0,
      orphans: 0,
      raw_missing: 1,
      pending_ai: 0,
      generated: 0,
      assigned: 1,
    },
    sponsor: {
      total: 2,
      by_status: { new: 1, audited: 1 },
      ignored: 0,
      orphans: 0,
      raw_missing: 0,
      pending_ai: 0,
      generated: 0,
      assigned: 2,
    },
    duplication: {
      total: 4,
      by_status: { pending: 2, resolved: 1, audited: 1 },
    },
  },
  diagnostics: {
    duplicate_groups: 3,
    dob_conflicts: 1,
    email_checks: 9,
    address_checks: 4,
  },
  artifacts: {},
  last_jobs: {},
  data_revision: '2026-11-01T10:00:00Z',
}

async function mountLanding() {
  return mountRouted(AuditLandingView, {
    path: '/audit',
    setup: () => {
      useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
    },
  })
}

function mockOverview() {
  mockRoutes([
    (url) =>
      url.includes('/overview') ? jsonResponse(200, OVERVIEW) : undefined,
    (url) =>
      url.includes('/diagnostics/')
        ? jsonResponse(200, [])
        : undefined,
  ])
}

beforeEach(() => setActivePinia(createPinia()))

describe('audit landing', () => {
  it('offers one card per audit section with that section progress', async () => {
    mockOverview()
    const { wrapper } = await mountLanding()
    await flushPromises()

    const sections = wrapper.get('[data-testid="audit-sections"]')
    const text = sections.text()
    expect(text).toContain('Children')
    expect(text).toContain('Families')
    expect(text).toContain('Sponsors')
    expect(text).toContain('Duplications')
    // The children's progress: 12 rows, 3 audited.
    expect(text).toContain('12')
    expect(text).toContain('audited')
    // The duplicate groups carry their own status vocabulary.
    expect(text).toContain('pending')
    expect(text).toContain('resolved')
  })

  it('links each card to its audit section', async () => {
    mockOverview()
    const { wrapper } = await mountLanding()
    await flushPromises()

    const targets = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .map((link) => link.props('to'))
    expect(targets).toContain('/audit/children')
    expect(targets).toContain('/audit/families')
    expect(targets).toContain('/audit/sponsors')
    expect(targets).toContain('/audit/duplications')
  })
})

describe('registry search', () => {
  const SEARCH_ROWS = {
    items: [
      {
        entity: 'family',
        key: '42',
        label: 'Doe family',
        status: 'audited',
        family_size: 3,
        sponsored: true,
        ignore: false,
      },
      {
        entity: 'child',
        key: 'z-1',
        label: 'Jane Doe',
        status: 'new',
        family_size: 3,
        sponsored: false,
        ignore: false,
      },
      {
        entity: 'sponsor',
        key: '77',
        label: 'Smith, John',
        status: 'in_progress',
        sponsored: true,
        ignore: false,
      },
    ],
    total: 3,
    page: 1,
    page_size: 50,
  }

  function mockSearch(
    rows: unknown = SEARCH_ROWS,
  ): { urls: string[]; reset: () => void } {
    const urls: string[] = []
    mockRoutes([
      (url) => {
        if (!url.includes('/search')) return undefined
        urls.push(url)
        return jsonResponse(200, rows)
      },
      (url) =>
        url.includes('/overview') ? jsonResponse(200, OVERVIEW) : undefined,
    ])
    return { urls, reset: () => (urls.length = 0) }
  }

  it('does not search until the user asks', async () => {
    const { urls } = mockSearch()
    const { wrapper } = await mountLanding()
    await flushPromises()

    expect(urls).toHaveLength(0)
    expect(wrapper.find('[data-testid="registry-results"]').exists()).toBe(false)
  })

  it('searches across entity kinds and links every row to its detail page', async () => {
    const { urls } = mockSearch()
    const { wrapper } = await mountLanding()
    await flushPromises()

    await wrapper.get('input[name="q"]').setValue('doe')
    await wrapper.get('form.filters button[type="submit"]').trigger('submit')
    await flushPromises()

    expect(urls).toHaveLength(1)
    const params = new URL(String(urls[0]!), 'http://localhost').searchParams
    expect(params.get('q')).toBe('doe')

    const links = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .map((link) => String(link.props('to')))
    expect(links).toContain('/audit/families/42')
    expect(links).toContain('/audit/children/z-1')
    expect(links).toContain('/audit/sponsors/77')
    expect(wrapper.get('[data-testid="registry-total"]').text()).toContain('3')
  })

  it('serializes the filters into the search request', async () => {
    const { urls } = mockSearch()
    const { wrapper } = await mountLanding()
    await flushPromises()

    await wrapper.get('input[name="q"]').setValue('smith')
    await flushPromises()
    urls.length = 0

    // Type filter: families/children/sponsors scoping.
    const selects = wrapper.findAll('form.filters select')
    await selects[0]!.setValue('child')
    await selects[1]!.setValue('audited')
    await selects[2]!.setValue('hancock')
    await selects[3]!.setValue('yes')
    await selects[4]!.setValue('no')
    await flushPromises()

    expect(urls).toHaveLength(1)
    const params = new URL(String(urls[0]!), 'http://localhost').searchParams
    expect(params.get('entity')).toBe('child')
    expect(params.get('status')).toBe('audited')
    expect(params.get('location')).toBe('hancock')
    expect(params.get('sponsored')).toBe('false')
  })

  it('renders an empty state and shows the pagination only when needed', async () => {
    mockSearch({ items: [], total: 0, page: 1, page_size: 50 })
    const { wrapper } = await mountLanding()
    await flushPromises()

    await wrapper.get('input[name="q"]').setValue('nothing')
    await wrapper.get('form.filters button[type="submit"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="registry-empty"]').text()).toContain(
      'Nothing',
    )
    expect(wrapper.find('[data-testid="registry-page"]').exists()).toBe(false)  })
})
