import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import ChildDetailView from '../src/views/ChildDetailView.vue'
import FamilyDetailView from '../src/views/FamilyDetailView.vue'
import SponsorDetailView from '../src/views/SponsorDetailView.vue'
import AuditListView from '../src/views/AuditListView.vue'
import AuditBackLink from '../src/components/AuditBackLink.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

function seedSeason() {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

function entityDetail(key: string, neighbours: Record<string, unknown> = {}) {
  return {
    entity: 'child',
    key,
    row: { zite_record_id: key, first_name: 'Kid', status: 'new' },
    baseline: {},
    submissions: [],
    status: 'new',
    row_version: 1,
    last_updated_by: null,
    last_updated_at: null,
    audited_at: null,
    diagnostics: {},
    ...neighbours,
  }
}

beforeEach(() => setActivePinia(createPinia()))

describe('audit navigation', () => {
  it('sends the child detail back to the children list with the filters', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1') ? jsonResponse(200, entityDetail('c1')) : undefined,
    ])
    const { wrapper } = await mountRouted(ChildDetailView, {
      path: '/audit/children/c1?status=audited&q=kid',
      setup: seedSeason,
    })
    await flushPromises()

    const back = wrapper.findComponent(AuditBackLink)
    expect(back.props('to')).toBe('/audit/children?status=audited&q=kid')
  })

  it('sends a child opened from a family back to that family', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1') ? jsonResponse(200, entityDetail('c1')) : undefined,
    ])
    const { wrapper } = await mountRouted(ChildDetailView, {
      path: '/audit/families/12/children/c1',
      setup: seedSeason,
    })
    await flushPromises()

    const back = wrapper.findComponent(AuditBackLink)
    expect(back.props('to')).toBe('/audit/families/12')
    expect(back.text()).toContain('Family 12')
  })

  it('sends the family and sponsor details back to their lists', async () => {
    mockRoutes([
      (url) =>
        url.includes('/families/1')
          ? jsonResponse(200, { ...entityDetail('1'), entity: 'family' })
          : url.includes('/sponsors/10')
            ? jsonResponse(200, { ...entityDetail('10'), entity: 'sponsor' })
            : undefined,
    ])
    const family = await mountRouted(FamilyDetailView, {
      path: '/audit/families/1?q=doe',
      setup: seedSeason,
    })
    await flushPromises()
    expect(family.wrapper.findComponent(AuditBackLink).props('to')).toBe(
      '/audit/families?q=doe',
    )

    const sponsor = await mountRouted(SponsorDetailView, {
      path: '/audit/sponsors/10',
      setup: seedSeason,
    })
    await flushPromises()
    expect(sponsor.wrapper.findComponent(AuditBackLink).props('to')).toBe(
      '/audit/sponsors',
    )
  })

  it('walks to the neighbouring entry, keeping the filters', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1')
          ? jsonResponse(
              200,
              entityDetail('c1', {
                previous: { key: 'c0', label: 'Previous Kid' },
                next: { key: 'c2', label: 'Next Kid' },
              }),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(ChildDetailView, {
      path: '/audit/children/c1?status=audited&q=kid',
      setup: seedSeason,
    })
    await flushPromises()

    const links = wrapper.findAllComponents({ name: 'RouterLinkStub' })
    const next = links.find(
      (link) => link.attributes('data-testid') === 'next-entry',
    )
    expect(next!.props('to')).toBe('/audit/children/c2?status=audited&q=kid')
    expect(next!.text()).toContain('Next Kid')
    const previous = links.find(
      (link) => link.attributes('data-testid') === 'previous-entry',
    )
    expect(previous!.props('to')).toBe(
      '/audit/children/c0?status=audited&q=kid',
    )
  })

  it('sends the audit list back to the audit landing', async () => {
    mockRoutes([
      (url) =>
        url.includes('/sponsors?')
          ? jsonResponse(200, { items: [], total: 0, page: 1, page_size: 50 })
          : undefined,
    ])
    const { wrapper } = await mountRouted(AuditListView, {
      path: '/audit/sponsors',
      props: { segment: 'sponsors' },
      setup: seedSeason,
    })
    await flushPromises()

    expect(wrapper.findComponent(AuditBackLink).props('to')).toBe('/audit')
  })
})
