import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import SponsorDetailView from '../src/views/SponsorDetailView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

function sponsorDetail() {
  return {
    entity: 'sponsor',
    key: '47',
    row: { sponsor_id: 47, first_name: 'Peyton', status: 'new', ignore: false },
    baseline: {},
    submissions: [],
    status: 'new',
    row_version: 1,
    last_updated_by: null,
    last_updated_at: null,
    audited_at: null,
    diagnostics: {},
  }
}

function childrenPage() {
  return {
    items: [
      {
        zite_record_id: 'c1',
        family_id: 8,
        first_name: 'Hazel',
        angel_id: '100A',
        status: 'audited',
        dob: '2016-03-02',
      },
    ],
    total: 1,
    page: 1,
    page_size: 200,
  }
}

function seedSeason() {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

beforeEach(() => setActivePinia(createPinia()))

describe('sponsor detail', () => {
  it('lists the assigned children and links each to its family', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/children?')
          ? jsonResponse(200, childrenPage())
          : url.includes('/sponsors/47')
            ? jsonResponse(200, sponsorDetail())
            : undefined,
    ])
    const { wrapper } = await mountRouted(SponsorDetailView, {
      path: '/audit/sponsors/47',
      setup: seedSeason,
    })
    await flushPromises()

    // The sponsor id is an exact filter on the children list.
    const requested = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(requested).toContain('sponsor_id=47')

    const card = wrapper.get('[data-testid="sponsor-children"]')
    expect(card.text()).toContain('Assigned children')
    const cells = card
      .findAll('tbody tr')[0]!
      .findAll('td')
      .map((cell) => cell.text())
    expect(cells).toContain('Hazel')
    expect(cells).toContain('100A')

    const targets = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .map((row) => row.props('to'))
    expect(targets).toContain('/audit/children/c1')
    expect(targets).toContain('/audit/families/8')
  })

  it('shows an empty state when no child is assigned', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children?')
          ? jsonResponse(200, {
              items: [],
              total: 0,
              page: 1,
              page_size: 200,
            })
          : url.includes('/sponsors/47')
            ? jsonResponse(200, sponsorDetail())
            : undefined,
    ])
    const { wrapper } = await mountRouted(SponsorDetailView, {
      path: '/audit/sponsors/47',
      setup: seedSeason,
    })
    await flushPromises()

    const card = wrapper.get('[data-testid="sponsor-children"]')
    expect(card.text()).toContain(
      'No children are assigned to this sponsor.',
    )
  })
})
