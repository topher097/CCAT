import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import MatchView from '../src/views/MatchView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

function page(items: unknown[]) {
  return jsonResponse(200, {
    items,
    total: items.length,
    page: 1,
    page_size: 200,
  })
}

function baseRoutes(report: Response) {
  return mockRoutes([
    (url) =>
      url.includes('/diagnostics/match-report') ? report : undefined,
    (url) =>
      url.includes('/seasons/2026/families?')
        ? page([
            {
              family_id: 1,
              first_name: 'Jane',
              assigned_sponsor_id: 9,
            },
            { family_id: 2, first_name: 'Una', assigned_sponsor_id: null },
          ])
        : undefined,
    (url) =>
      url.includes('/seasons/2026/sponsors?')
        ? page([
            { sponsor_id: 9, first_name: 'Mary', assigned_angel_ids: '1A,1B' },
            { sponsor_id: 10, first_name: 'Empty', assigned_angel_ids: '' },
          ])
        : undefined,
  ])
}

beforeEach(() => setActivePinia(createPinia()))

describe('match results', () => {
  it('renders the latest match report rows', async () => {
    baseRoutes(
      jsonResponse(200, [
        { kind: 'unmatched_family', ref: '1', reason: 'no sponsor capacity' },
      ]),
    )
    const { wrapper } = await mountRouted(MatchView, {
      path: '/match',
      setup: withSeason,
    })
    await flushPromises()

    const rows = wrapper.get('[data-testid="match-report"]').findAll('tbody tr')
    expect(rows).toHaveLength(1)
    expect(rows[0]!.text()).toContain('unmatched_family')
    expect(rows[0]!.text()).toContain('no sponsor capacity')
  })

  it('shows the explicit empty state on a 404 report', async () => {
    baseRoutes(
      jsonResponse(404, {
        detail: { code: 'not_found', message: 'no match run exists' },
      }),
    )
    const { wrapper } = await mountRouted(MatchView, {
      path: '/match',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="match-report-empty"]').text()).toContain(
      'No match report yet',
    )
    expect(wrapper.find('[data-testid="match-report"]').exists()).toBe(false)
  })

  it('derives assigned sponsors and families from the audit lists', async () => {
    baseRoutes(jsonResponse(200, []))
    const { wrapper } = await mountRouted(MatchView, {
      path: '/match',
      setup: withSeason,
    })
    await flushPromises()

    const sponsors = wrapper
      .get('[data-testid="assigned-sponsors"]')
      .findAll('tbody tr')
    expect(sponsors).toHaveLength(1)
    expect(sponsors[0]!.text()).toContain('9')
    expect(sponsors[0]!.text()).toContain('1A,1B')

    const families = wrapper
      .get('[data-testid="assigned-families"]')
      .findAll('tbody tr')
    expect(families).toHaveLength(1)
    expect(families[0]!.text()).toContain('Jane')
  })
})
