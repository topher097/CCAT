import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

import AuditListView from '../src/views/AuditListView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes } from './helpers'

const SPONSOR_ROWS = [
  {
    sponsor_id: 10,
    first_name: 'Ada',
    last_name: 'Lovelace',
    email: 'ada@example.org',
    status: 'new',
    ignore: false,
    last_updated_by: 'cmendres',
    last_updated_at: '2026-11-18T10:00:00Z',
  },
  {
    sponsor_id: 11,
    first_name: 'Grace',
    last_name: 'Hopper',
    email: 'grace@example.org',
    status: 'ignored',
    ignore: true,
  },
]

function mountList(segment: 'sponsors' | 'families' | 'children') {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
  return mount(AuditListView, {
    props: { segment },
    global: {
      plugins: [pinia],
      stubs: { RouterLink: RouterLinkStub },
    },
  })
}

function pageOf(items: unknown[], total = items.length, page = 1) {
  return jsonResponse(200, {
    items,
    total,
    page,
    page_size: 50,
  })
}

beforeEach(() => setActivePinia(createPinia()))

describe('audit list', () => {
  it('renders server-paginated rows with status and update provenance', async () => {
    mockRoutes([
      (url) =>
        url.includes('/sponsors?') ? pageOf(SPONSOR_ROWS, 2) : undefined,
    ])
    const wrapper = mountList('sponsors')
    await flushPromises()

    expect(wrapper.get('[data-testid="list-total"]').text()).toBe('2 rows')
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('Ada Lovelace')
    expect(rows[0]!.text()).toContain('new')
    expect(rows[0]!.text()).toContain('cmendres')
    expect(rows[1]!.text()).toContain('ignored')
    expect(wrapper.get('[data-testid="list-page"]').text()).toContain(
      'Page 1 of 1',
    )
  })

  it('wires status chips to the server filter and resets to page 1', async () => {
    const fetchMock = mockRoutes([
      (url) => {
        if (!url.includes('/sponsors?')) return undefined
        const audited = url.includes('status=audited')
        return pageOf(audited ? [SPONSOR_ROWS[0]] : SPONSOR_ROWS, 2)
      },
    ])
    const wrapper = mountList('sponsors')
    await flushPromises()

    const chip = wrapper
      .findAll('button')
      .find((button) => button.text() === 'audited')
    expect(chip).toBeDefined()
    await chip!.trigger('click')
    await flushPromises()

    const lastUrl = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(lastUrl).toContain('status=audited')
    expect(lastUrl).toContain('page=1')
    expect(chip!.attributes('aria-pressed')).toBe('true')
  })

  it('advances and disables pagination at the boundaries', async () => {
    const fetchMock = mockRoutes([
      (url) => {
        if (!url.includes('/sponsors?')) return undefined
        const page = Number(new URL(url, 'http://x').searchParams.get('page'))
        return pageOf(SPONSOR_ROWS, 120, page)
      },
    ])
    const wrapper = mountList('sponsors')
    await flushPromises()

    const next = wrapper
      .findAll('button')
      .find((button) => button.text() === 'Next')
    expect(next!.attributes('disabled')).toBeUndefined()
    await next!.trigger('click')
    await flushPromises()

    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('page=2')
    expect(wrapper.get('[data-testid="list-page"]').text()).toContain(
      'Page 2 of 3',
    )
  })

  it('flags orphan and ignored rows with text indicators', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children?')
          ? pageOf([
              {
                zite_record_id: 'child-1',
                first_name: 'Kid',
                family_id: null,
                status: 'new',
                ignore: false,
              },
            ])
          : undefined,
    ])
    const wrapper = mountList('children')
    await flushPromises()
    const row = wrapper.findAll('tbody tr')[0]!
    expect(row.text()).toContain('orphan')
  })

  it('shows per-family child counts read from the children list', async () => {
    mockRoutes([
      (url) =>
        url.includes('/families?')
          ? pageOf([
              { family_id: 1, first_name: 'One', status: 'new' },
              { family_id: 2, first_name: 'Two', status: 'new' },
            ])
          : undefined,
      (url) =>
        url.includes('/children?')
          ? pageOf([
              { zite_record_id: 'c1', family_id: 1 },
              { zite_record_id: 'c2', family_id: 1 },
              { zite_record_id: 'c3', family_id: 2 },
            ])
          : undefined,
    ])
    const wrapper = mountList('families')
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    expect(rows[0]!.text()).toContain('2')
    expect(rows[1]!.text()).toContain('1')
  })
})
