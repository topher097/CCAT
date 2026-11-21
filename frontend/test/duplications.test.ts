import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import DuplicationListView from '../src/views/DuplicationListView.vue'
import DuplicationDetailView from '../src/views/DuplicationDetailView.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const GROUPS = [
  {
    id: 'g1',
    sheet: 'duplicates',
    entity: 'child',
    group_key: 'key-1',
    member_count: 2,
    decided_count: 0,
    status: 'pending',
    survivor_record_id: 'r1',
    conflict_columns: ['first_name'],
    created: '2026-11-18T10:00:00Z',
  },
  {
    id: 'g2',
    sheet: 'duplicates',
    entity: 'family',
    group_key: 'key-2',
    member_count: 3,
    decided_count: 3,
    status: 'resolved',
    survivor_record_id: 'r9',
    conflict_columns: [],
    created: null,
  },
]

function member(overrides: Record<string, unknown> = {}) {
  return {
    zite_record_id: 'r1',
    created: '2026-11-18T10:00:00Z',
    survivor: true,
    conflict_columns: ['first_name'],
    conflict_details: 'first_name: Ann vs Anne',
    raw: { first_name: 'Ann', dob: '2018-01-01' },
    screened: {
      zite_record_id: 'r1',
      first_name: 'Ann',
      status: 'new',
      row_version: 3,
    },
    decision: null,
    assigned_id: null,
    decided_by: null,
    decided_at: null,
    row_version: 3,
    ...overrides,
  }
}

function detail(members: unknown[] = [member()]) {
  return {
    id: 'g1',
    sheet: 'duplicates',
    entity: 'child',
    group_key: 'key-1',
    status: 'pending',
    members,
  }
}

function seedSeason() {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

beforeEach(() => setActivePinia(createPinia()))

describe('duplication list', () => {
  it('renders one row per group and links to the detail', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications?')
          ? jsonResponse(200, {
              items: GROUPS,
              total: 2,
              page: 1,
              page_size: 50,
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationListView, {
      path: '/audit/duplications',
      setup: seedSeason,
    })
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('key-1')
    expect(rows[0]!.text()).toContain('pending')
    expect(rows[0]!.text()).toContain('first_name')
    expect(rows[1]!.text()).toContain('resolved')
    const groupLink = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .find((link) => link.text() === 'key-1')
    expect(groupLink!.props('to')).toBe('/audit/duplications/g1')
    expect(wrapper.get('[data-testid="list-page"]').text()).toContain(
      'Page 1 of 1',
    )
  })
})

describe('duplication detail', () => {
  it('renders one row per field and one column per submission', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member(),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  raw: { first_name: 'Anne', email: 'anne@example.org' },
                  screened: null,
                }),
              ]),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // Rows are fields; columns are the raw submissions plus the current one.
    const rows = wrapper.findAll('tbody tr')
    expect(rows.map((row) => row.attributes('data-field'))).toEqual([
      'first_name',
      'dob',
      'email',
    ])
    const headers = wrapper.findAll('thead th')
    expect(headers).toHaveLength(4)
    expect(headers[1]!.text()).toContain('r1')
    expect(headers[2]!.text()).toContain('r2')
    expect(headers[3]!.text()).toContain('Current (editable)')

    // The conflicting field shows both raw values and is marked.
    const conflict = wrapper.get('[data-field="first_name"]')
    expect(conflict.text()).toContain('Ann')
    expect(conflict.text()).toContain('Anne')
    expect(conflict.text()).toContain('conflict')
    expect(conflict.classes()).toContain('diff-changed')
    // A field one submission leaves blank does not conflict.
    const dob = wrapper.get('[data-field="dob"]')
    expect(dob.classes()).not.toContain('diff-changed')
    expect(dob.text()).not.toContain('conflict')
    // The current column holds the screened value.
    const current = wrapper.get('input[name="first_name"]')
    expect((current.element as HTMLInputElement).value).toBe('Ann')
    // Two checkboxes per submission, in the column header.
    expect(wrapper.findAll('thead input[type="checkbox"]')).toHaveLength(4)
  })

  it('holds a not_duplicate decision until the page is saved', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/members/r1'))
          return jsonResponse(
            200,
            detail([
              member({
                decision: 'not_duplicate',
                assigned_id: '1B',
                decided_by: 'cmendres',
              }),
            ]),
          )
        if (url.includes('/duplications/g1')) return jsonResponse(200, detail())
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="not-duplicate"]').trigger('click')
    await flushPromises()

    // The toggle is a pending edit: nothing is posted yet.
    expect(
      fetchMock.mock.calls.some(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toBe(false)
    expect(wrapper.find('[data-testid="pending-decision"]').exists()).toBe(true)
    expect(
      (wrapper.get('[data-testid="not-duplicate"]').element as HTMLInputElement)
        .checked,
    ).toBe(true)

    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(String(post![0])).toBe(
      '/api/v1/seasons/2026/duplications/g1/members/r1',
    )
    expect(bodyOf(post as unknown[])).toEqual({
      decision: 'not_duplicate',
      confirm: true,
      expected_version: 3,
    })
    expect(wrapper.find('[data-testid="pending-decision"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'Saved',
    )
  })

  it('confirms ignore before holding it, and saves nothing on cancel', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/members/r1'))
          return jsonResponse(200, detail([member({ decision: 'ignore' })]))
        if (url.includes('/duplications/g1')) return jsonResponse(200, detail())
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="ignore"]').trigger('click')
    expect(wrapper.find('[data-testid="confirm"]').exists()).toBe(true)

    const dialog = wrapper.get('[role="dialog"]')
    await dialog.findAll('button')[0]!.trigger('click') // Cancel
    expect(wrapper.find('[data-testid="confirm"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pending-decision"]').exists()).toBe(false)
    expect(
      fetchMock.mock.calls.some(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toBe(false)

    await wrapper.get('[data-testid="ignore"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="pending-decision"]').exists()).toBe(true)

    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(bodyOf(post as unknown[])).toEqual({
      decision: 'ignore',
      confirm: true,
      expected_version: 3,
    })
    expect(
      (wrapper.get('[data-testid="ignore"]').element as HTMLInputElement)
        .checked,
    ).toBe(true)
  })

  it('marks the group audited once every member is resolved', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            { ...detail(), status: 'audited', audited_at: '2026-11-20T10:00:00Z' },
          )
        if (init.method === 'POST' && url.endsWith('/members/r1'))
          return jsonResponse(200, detail([member({ decision: 'not_duplicate' })]))
        if (url.includes('/duplications/g1')) return jsonResponse(200, detail())
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // Unresolved members keep the button disabled.
    expect(
      wrapper.get('[data-testid="mark-audited"]').attributes('disabled'),
    ).toBeDefined()

    await wrapper.get('[data-testid="not-duplicate"]').trigger('click')
    await flushPromises()
    // An unsaved decision keeps the button disabled too.
    expect(
      wrapper.get('[data-testid="mark-audited"]').attributes('disabled'),
    ).toBeDefined()

    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()
    // The member is resolved now, so the group can be audited.
    expect(
      wrapper.get('[data-testid="mark-audited"]').attributes('disabled'),
    ).toBeUndefined()

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) =>
        (call[1] as RequestInit).method === 'POST' &&
        String(call[0]).endsWith('/audit'),
    )
    expect(String(post![0])).toBe('/api/v1/seasons/2026/duplications/g1/audit')
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'marked audited',
    )
  })

  it('saves the edited current values onto the screened row', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'PATCH' && url.includes('/children/r1'))
          return jsonResponse(200, {
            entity: 'child',
            key: 'r1',
            row: {
              zite_record_id: 'r1',
              first_name: 'Annie',
              status: 'new',
            },
            baseline: {},
            submissions: [],
            status: 'new',
            row_version: 4,
            last_updated_by: 'cmendres',
            last_updated_at: '2026-11-20T10:00:00Z',
            audited_at: null,
            diagnostics: {},
          })
        if (url.includes('/duplications/g1')) return jsonResponse(200, detail())
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const input = wrapper.get('input[name="first_name"]')
    await input.setValue('Annie')
    expect(wrapper.get('[data-testid="save"]').attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const patch = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'PATCH',
    )
    expect(String(patch![0])).toBe('/api/v1/seasons/2026/children/r1')
    expect(bodyOf(patch as unknown[])).toEqual({ first_name: 'Annie' })
    const headers = (patch![1] as RequestInit).headers as Record<string, string>
    expect(headers['If-Match']).toBe('3')
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'Saved',
    )
  })
})
