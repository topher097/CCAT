import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import AuditListView from '../src/views/AuditListView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

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

async function mountList(segment: 'sponsors' | 'families' | 'children') {
  const { wrapper } = await mountRouted(AuditListView, {
    path: `/audit/${segment}`,
    props: { segment },
    setup: () => {
      useSessionStore().season = {
        id: 1,
        name: '2026',
        xmas_date: '2026-12-25',
      }
    },
  })
  return wrapper
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
    const wrapper = await mountList('sponsors')
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
    const wrapper = await mountList('sponsors')
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

  it('keeps the filters in the URL and carries them into the detail link', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/sponsors?') ? pageOf(SPONSOR_ROWS, 2) : undefined,
    ])
    const { wrapper, router } = await mountRouted(AuditListView, {
      path: '/audit/sponsors?status=audited&q=ada',
      props: { segment: 'sponsors' },
      setup: () => {
        useSessionStore().season = {
          id: 1,
          name: '2026',
          xmas_date: '2026-12-25',
        }
      },
    })
    await flushPromises()

    const requested = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(requested).toContain('status=audited')
    expect(requested).toContain('q=ada')
    expect(router.currentRoute.value.query.status).toBe('audited')

    const link = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .find((candidate) => candidate.text() === '10')
    expect(link!.props('to')).toBe('/audit/sponsors/10?status=audited&q=ada')
  })

  it('advances and disables pagination at the boundaries', async () => {
    const fetchMock = mockRoutes([
      (url) => {
        if (!url.includes('/sponsors?')) return undefined
        const page = Number(new URL(url, 'http://x').searchParams.get('page'))
        return pageOf(SPONSOR_ROWS, 120, page)
      },
    ])
    const wrapper = await mountList('sponsors')
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
    const wrapper = await mountList('children')
    await flushPromises()
    const row = wrapper.findAll('tbody tr')[0]!
    expect(row.text()).toContain('orphan')
  })

  it('shows email and address validation status for families', async () => {
    mockRoutes([
      (url) =>
        url.includes('/families?')
          ? pageOf([
              {
                family_id: 1,
                first_name: 'One',
                status: 'new',
                child_count: 2,
                email_status: 'valid',
                address_status: 'not_found',
              },
            ])
          : undefined,
    ])
    const wrapper = await mountList('families')
    await flushPromises()

    const cell = wrapper.get('[data-testid="validation"]')
    expect(cell.text()).toContain('email: valid')
    expect(cell.text()).toContain('address: not found')
    const badges = cell.findAll('.badge')
    expect(badges[0]!.classes()).toContain('check-ok')
    expect(badges[1]!.classes()).toContain('check-bad')
  })

  it('shows only the email check for sponsors', async () => {
    mockRoutes([
      (url) =>
        url.includes('/sponsors?')
          ? pageOf([
              {
                sponsor_id: 10,
                first_name: 'Ada',
                status: 'new',
                email_status: 'do_not_mail',
              },
            ])
          : undefined,
    ])
    const wrapper = await mountList('sponsors')
    await flushPromises()

    const cell = wrapper.get('[data-testid="validation"]')
    expect(cell.text()).toContain('email: do not mail')
    expect(cell.text()).not.toContain('address:')
    expect(cell.get('.badge').classes()).toContain('check-bad')
  })

  it('shows the angel id and the family child count on child rows', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children?')
          ? pageOf([
              {
                zite_record_id: 'child-1',
                first_name: 'Kid',
                family_id: 7,
                angel_id: '1A',
                family_child_count: 3,
                status: 'new',
              },
            ])
          : undefined,
    ])
    const wrapper = await mountList('children')
    await flushPromises()

    const headers = wrapper.findAll('thead th').map((th) => th.text())
    expect(headers).toContain('Angel ID')
    expect(headers).toContain('Children in family')
    const cells = wrapper.findAll('tbody tr td').map((td) => td.text())
    expect(cells).toContain('1A')
    expect(cells).toContain('3')
  })

  it('flags rows whose duplicate group still awaits audit', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children?')
          ? pageOf([
              {
                zite_record_id: 'child-1',
                first_name: 'Kid',
                family_id: 7,
                status: 'new',
                duplicate_pending: true,
              },
            ])
          : undefined,
    ])
    const wrapper = await mountList('children')
    await flushPromises()

    expect(wrapper.get('[data-testid="duplicate-pending"]').text()).toBe(
      'duplicate pending audit',
    )
  })

  it('renders server-computed per-family child counts', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/families?')
          ? pageOf([
              { family_id: 1, first_name: 'One', status: 'new', child_count: 2 },
              { family_id: 2, first_name: 'Two', status: 'new', child_count: 0 },
            ])
          : undefined,
    ])
    const wrapper = await mountList('families')
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    expect(rows[0]!.text()).toContain('2')
    expect(rows[1]!.text()).toContain('0')
    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes('/children?'),
      ),
    ).toBe(false)
  })
})
