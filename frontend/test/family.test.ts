import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import FamilyDetailView from '../src/views/FamilyDetailView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

function familyDetail() {
  return {
    entity: 'family',
    key: '1',
    row: { family_id: 1, first_name: 'One', status: 'new', ignore: false },
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

function seedSeason() {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

beforeEach(() => setActivePinia(createPinia()))

describe('family detail', () => {
  it('lists the children with their angel id and family child count', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/children?')
          ? jsonResponse(200, {
              items: [
                {
                  zite_record_id: 'c1',
                  family_id: 1,
                  first_name: 'Kid',
                  angel_id: '2B',
                  family_child_count: 4,
                  status: 'new',
                },
              ],
              total: 1,
              page: 1,
              page_size: 200,
            })
          : url.includes('/families/1')
            ? jsonResponse(200, familyDetail())
            : undefined,
    ])
    const { wrapper } = await mountRouted(FamilyDetailView, {
      path: '/audit/families/1',
      setup: seedSeason,
    })
    await flushPromises()

    // The family id is an exact filter, not a search substring.
    const requested = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(requested).toContain('family_id=1')
    expect(requested).not.toContain('q=')

    const card = wrapper.get('[data-testid="family-children"]')
    const headers = card.findAll('th').map((cell) => cell.text())
    expect(headers).toContain('Angel ID')
    expect(headers).toContain('Children in family')

    const cells = card
      .findAll('tbody tr')[0]!
      .findAll('td')
      .map((cell) => cell.text())
    expect(cells).toContain('2B')
    expect(cells).toContain('4')
  })
})
