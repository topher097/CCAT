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
    kind: 'duplicate_submission',
    group_key: 'key-1',
    member_count: 2,
    decided_count: 0,
    survivor_count: 2,
    released_count: 0,
    status: 'pending',
    survivor_record_id: 'r1',
    conflict_columns: ['first_name'],
    created: '2026-11-18T10:00:00Z',
  },
  {
    id: 'g2',
    sheet: 'duplicates',
    entity: 'family',
    kind: 'duplicate_submission',
    group_key: 'key-2',
    member_count: 3,
    decided_count: 3,
    survivor_count: 1,
    released_count: 1,
    status: 'resolved',
    survivor_record_id: 'r9',
    conflict_columns: [],
    created: null,
  },
]

function member(overrides: Record<string, unknown> = {}) {
  const base = {
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
    state: 'surviving',
    row_key: 'r1',
    row_status: 'new',
    children: [],
  }
  const merged = { ...base, ...overrides }
  if (!('state' in overrides)) {
    if (merged.decision === 'ignore') merged.state = 'discarded'
    else if (merged.decision === 'not_duplicate') merged.state = 'released'
    else merged.state = 'surviving'
  }
  if (!('row_key' in overrides)) {
    merged.row_key = String(merged.zite_record_id)
  }
  return merged
}

function detail(
  members: unknown[] = [member()],
  overrides: Record<string, unknown> = {},
) {
  const survivorCount = (members as { decision: string | null }[]).filter(
    (m) => m.decision === null,
  ).length
  return {
    id: 'g1',
    sheet: 'duplicates',
    entity: 'child',
    group_key: 'key-1',
    status: 'pending',
    members,
    survivor_count: survivorCount,
    children: [],
    ...overrides,
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
    expect(rows[0]!.text()).toContain('2 survivors')
    expect(rows[1]!.text()).toContain('resolved')
    expect(rows[1]!.text()).toContain('1 survivor')
    expect(rows[1]!.text()).toContain('1 released member still needs its own audit')
    const groupLink = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .find((link) => link.text() === 'key-1')
    expect(groupLink!.props('to')).toBe('/audit/duplications/g1')
    expect(wrapper.get('[data-testid="list-page"]').text()).toContain(
      'Page 1 of 1',
    )
  })

  it('pages through the groups with the previous/next buttons', async () => {
    const fetchMock = mockRoutes([
      (url) => {
        if (!url.includes('/duplications?')) return undefined
        const page = Number(new URL(url, 'http://x').searchParams.get('page'))
        return jsonResponse(200, {
          items: GROUPS,
          total: 120,
          page,
          page_size: 50,
        })
      },
    ])
    const { wrapper } = await mountRouted(DuplicationListView, {
      path: '/audit/duplications',
      setup: seedSeason,
    })
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

  it('deselects a saved not_duplicate by queueing a clear decision', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/members/r1'))
          return jsonResponse(200, detail([member({ decision: null })]))
        if (url.includes('/duplications/g1'))
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
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // The checkbox reflects the stored decision and starts unchecked-pending.
    const box = wrapper.get('[data-testid="not-duplicate"]')
    await box.trigger('click')
    await flushPromises()

    // Clicking the stored decision queues a clear (checked -> unchecked).
    expect(
      (wrapper.get('[data-testid="not-duplicate"]').element as HTMLInputElement)
        .checked,
    ).toBe(false)
    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(bodyOf(post as unknown[])).toEqual({
      decision: 'clear',
      confirm: true,
      expected_version: 3,
    })
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

  it('marks the group audited when exactly one member is undecided', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            detail(
              [
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'ignore',
                  state: 'discarded',
                  row_key: 'r2',
                  raw: { first_name: 'Anne' },
                  screened: null,
                }),
              ],
              { status: 'audited', audited_at: '2026-11-20T10:00:00Z' },
            ),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'ignore',
                state: 'discarded',
                row_key: 'r2',
                raw: { first_name: 'Anne' },
                screened: null,
              }),
            ]),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // Exactly one undecided member keeps the button enabled.
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
    expect(bodyOf(post as unknown[])).toEqual({ expected_version: 3 })
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'marked audited',
    )
  })

  it('opens a blocker dialog and posts nothing when two members are undecided', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  raw: { first_name: 'Anne' },
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

    expect(
      wrapper.get('[data-testid="mark-audited"]').attributes('disabled'),
    ).toBeUndefined()

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    const dialog = wrapper.get('[role="dialog"]')
    expect(dialog.text()).toContain('This group cannot be audited yet')
    expect(dialog.text()).toContain(
      '2 submissions are still undecided — this group has more than one surviving entry',
    )
    expect(
      fetchMock.mock.calls.some(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toBe(false)

    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
  })

  it('labels each member state and marks the flagged one as suggested survivor', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '1B',
                  state: 'released',
                  row_key: 'r2',
                  raw: { first_name: 'Anne' },
                  screened: { zite_record_id: 'r2', first_name: 'Anne', status: 'new' },
                }),
                member({
                  zite_record_id: 'r3',
                  survivor: false,
                  decision: 'ignore',
                  state: 'discarded',
                  row_key: 'r3',
                  raw: { first_name: 'Anna' },
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

    const states = wrapper.findAll('[data-testid="member-state"]')
    expect(states.map((el) => el.text())).toEqual([
      'surviving entry',
      'released — its own entry now',
      'discarded',
    ])

    const suggested = wrapper.findAll('[data-testid="suggested-survivor"]')
    expect(suggested).toHaveLength(1)
    expect(suggested[0]!.text()).toBe('suggested survivor')
    expect(suggested[0]!.attributes('data-member')).toBe('r1')
  })

  it('keeps both decisions on the flagged member, including ignore', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'ignore',
                  state: 'discarded',
                  row_key: 'r2',
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

    const r1Header = wrapper.find('th[data-member="r1"]')
    expect(r1Header!.find('[data-testid="not-duplicate"]').exists()).toBe(true)
    expect(r1Header!.find('[data-testid="ignore"]').exists()).toBe(true)

    await r1Header!.find('[data-testid="ignore"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(
      (r1Header!.find('[data-testid="ignore"]').element as HTMLInputElement)
        .checked,
    ).toBe(true)
    expect(r1Header!.find('[data-testid="pending-decision"]').exists()).toBe(
      true,
    )
    expect(
      fetchMock.mock.calls.some(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toBe(false)
  })

  it('Save and mark audited sends current column values with the audit request', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            detail(
              [
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '1B',
                  state: 'released',
                  row_key: 'r2',
                }),
              ],
              { status: 'audited', audited_at: '2026-11-20T10:00:00Z' },
            ),
          )
        if (init.method === 'POST' && url.endsWith('/members/r2'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'not_duplicate',
                assigned_id: '1B',
                state: 'released',
                row_key: 'r2',
              }),
            ]),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'ignore',
                state: 'discarded',
                row_key: 'r2',
              }),
            ]),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // r2 starts discarded; toggle it to released so there is still one survivor.
    const r2Header = wrapper.find('th[data-member="r2"]')
    await r2Header!.find('[data-testid="not-duplicate"]').trigger('click')
    await flushPromises()

    await wrapper.get('input[name="first_name"]').setValue('Annie')
    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="dialog"]').text()).toContain('Unsaved changes')
    expect(wrapper.find('[role="dialog"]').text()).toContain(
      "The member decisions are saved either way; the current column's edits are kept only by 'Save and mark audited'",
    )

    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const posts = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(posts.map((call) => String(call[0]))).toEqual([
      '/api/v1/seasons/2026/duplications/g1/members/r2',
      '/api/v1/seasons/2026/duplications/g1/audit',
    ])
    expect(bodyOf(posts[0] as unknown[])).toEqual({
      decision: 'not_duplicate',
      confirm: true,
      expected_version: 3,
    })
    // The whole editable column is sent, the untouched cells with their merged value.
    expect(bodyOf(posts[1] as unknown[])).toEqual({
      values: { first_name: 'Annie', dob: null },
      expected_version: 3,
    })
  })

  it('Mark audited without saving sends the audit request with no values', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            detail(
              [
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '1B',
                  state: 'released',
                  row_key: 'r2',
                }),
              ],
              { status: 'audited', audited_at: '2026-11-20T10:00:00Z' },
            ),
          )
        if (init.method === 'POST' && url.endsWith('/members/r2'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'not_duplicate',
                assigned_id: '1B',
                state: 'released',
                row_key: 'r2',
              }),
            ]),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'ignore',
                state: 'discarded',
                row_key: 'r2',
              }),
            ]),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const r2Header = wrapper.find('th[data-member="r2"]')
    await r2Header!.find('[data-testid="not-duplicate"]').trigger('click')
    await wrapper.get('input[name="first_name"]').setValue('Annie')

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="alternate"]').trigger('click')
    await flushPromises()

    const posts = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(posts.map((call) => String(call[0]))).toEqual([
      '/api/v1/seasons/2026/duplications/g1/members/r2',
      '/api/v1/seasons/2026/duplications/g1/audit',
    ])
    const auditBody = bodyOf(posts[1] as unknown[])
    expect(auditBody).not.toHaveProperty('values')
    expect(auditBody).toEqual({ expected_version: 3 })
  })

  it('shows a released member label and a link to its own row', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '1B',
                  state: 'released',
                  row_key: 'r2',
                  row_status: 'new',
                  raw: { first_name: 'Anne' },
                  screened: { zite_record_id: 'r2', first_name: 'Anne', status: 'new' },
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

    const r2Header = wrapper.find('th[data-member="r2"]')
    expect(r2Header!.text()).toContain('released — its own entry now')
    const link = r2Header!.findComponent({ name: 'RouterLinkStub' })
    expect(link.text()).toBe('Open its own row — still needs its own audit')
    expect(link.props('to')).toBe('/audit/children/r2')
  })

  it('asks what to do with unsaved changes before auditing the group', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            detail(
              [
                member({ zite_record_id: 'r1', survivor: true }),
                member({
                  zite_record_id: 'r2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '1B',
                  state: 'released',
                  row_key: 'r2',
                }),
              ],
              { status: 'audited', audited_at: '2026-11-20T10:00:00Z' },
            ),
          )
        if (init.method === 'POST' && url.endsWith('/members/r2'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'not_duplicate',
                assigned_id: '1B',
                state: 'released',
                row_key: 'r2',
              }),
            ]),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail([
              member({ zite_record_id: 'r1', survivor: true }),
              member({
                zite_record_id: 'r2',
                survivor: false,
                decision: 'ignore',
                state: 'discarded',
                row_key: 'r2',
              }),
            ]),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const r2Header = wrapper.find('th[data-member="r2"]')
    await r2Header!.find('[data-testid="not-duplicate"]').trigger('click')
    expect(
      wrapper.get('[data-testid="mark-audited"]').attributes('disabled'),
    ).toBeUndefined()

    // Cancel changes nothing.
    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    expect(wrapper.get('[role="dialog"]').text()).toContain('Unsaved changes')
    await wrapper
      .findAll('[role="dialog"] button')
      .find((button) => button.text() === 'Cancel')!
      .trigger('click')
    await flushPromises()
    expect(
      fetchMock.mock.calls.some(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toBe(false)

    // Save and mark audited saves the decision first, then audits.
    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const posts = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit).method === 'POST',
    )
    expect(posts.map((call) => String(call[0]))).toEqual([
      '/api/v1/seasons/2026/duplications/g1/members/r2',
      '/api/v1/seasons/2026/duplications/g1/audit',
    ])
    expect(bodyOf(posts[0] as unknown[])).toEqual({
      decision: 'not_duplicate',
      confirm: true,
      expected_version: 3,
    })
    expect(bodyOf(posts[1] as unknown[])).toEqual({
      values: { first_name: 'Ann', dob: null },
      expected_version: 3,
    })
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'marked audited',
    )
  })

  it('reloads the group when the route parameter changes', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(200, { ...detail(), group_key: 'key-1' })
          : url.includes('/duplications/g2')
            ? jsonResponse(200, {
                ...detail(),
                id: 'g2',
                group_key: 'key-2',
                entity: 'family',
              })
            : undefined,
    ])
    const { wrapper, router } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()
    expect(wrapper.get('h1').text()).toContain('key-1')

    await router.push('/audit/duplications/g2')
    await flushPromises()

    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes('/duplications/g2'),
      ),
    ).toBe(true)
    expect(wrapper.get('h1').text()).toContain('key-2')
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
    expect(bodyOf(patch as unknown[])).toEqual({
      first_name: 'Annie',
      dob: null,
    })
    const headers = (patch![1] as RequestInit).headers as Record<string, string>
    expect(headers['If-Match']).toBe('3')
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'Saved',
    )
  })

  it('shows the current binding line and makes the column read-only on an audited row', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member({
                  zite_record_id: 'r1',
                  survivor: true,
                  row_status: 'audited',
                  screened: {
                    zite_record_id: 'r1',
                    first_name: 'Ann',
                    status: 'audited',
                    row_version: 3,
                  },
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

    expect(wrapper.get('[data-testid="current-binding"]').text()).toBe(
      'Current column: the surviving entry, r1.',
    )
    expect(wrapper.get('[data-testid="current-readonly"]').text()).toBe(
      'This row is audited — its values are final.',
    )
    expect(
      (wrapper.get('input[name="first_name"]').element as HTMLInputElement)
        .readOnly,
    ).toBe(true)
  })

  it('keeps the Re-audit button enabled for an audited group', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(
            200,
            detail(
              [member({ zite_record_id: 'r1', survivor: true })],
              {
                status: 'audited',
                audited_at: '2026-11-20T10:00:00Z',
              },
            ),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail([member({ zite_record_id: 'r1', survivor: true })], {
              status: 'audited',
              audited_at: '2026-11-20T10:00:00Z',
            }),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const button = wrapper.get('[data-testid="mark-audited"]')
    expect(button.text()).toBe('Re-audit')
    expect(button.attributes('disabled')).toBeUndefined()

    await button.trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) =>
        (call[1] as RequestInit).method === 'POST' &&
        String(call[0]).endsWith('/audit'),
    )
    expect(post).toBeDefined()
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'marked audited',
    )
  })

  it('lists children for a family group and saves family assignments', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'POST' && url.endsWith('/children'))
          return jsonResponse(
            200,
            detail(
              [
                member({
                  zite_record_id: 'f1',
                  survivor: true,
                  row_key: '10',
                }),
                member({
                  zite_record_id: 'f2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '20',
                  state: 'released',
                  row_key: '20',
                }),
              ],
              {
                entity: 'family',
                status: 'resolved',
                children: [
                  {
                    zite_record_id: 'c1',
                    first_name: 'Ann',
                    dob: '2018-01-01',
                    family_id: 20,
                    angel_id: '20A',
                  },
                ],
              },
            ),
          )
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail(
              [
                member({
                  zite_record_id: 'f1',
                  survivor: true,
                  row_key: '10',
                }),
                member({
                  zite_record_id: 'f2',
                  survivor: false,
                  decision: 'not_duplicate',
                  assigned_id: '20',
                  state: 'released',
                  row_key: '20',
                }),
              ],
              {
                entity: 'family',
                status: 'resolved',
                children: [
                  {
                    zite_record_id: 'c1',
                    first_name: 'Ann',
                    dob: '2018-01-01',
                    family_id: 10,
                    angel_id: '10A',
                  },
                ],
              },
            ),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const childrenSection = wrapper.get('[data-testid="group-children"]')
    expect(childrenSection.text()).toContain('Ann')
    expect(childrenSection.text()).toContain('2018-01-01')
    expect(childrenSection.text()).toContain('10')

    const select = wrapper.get('[data-testid="child-family-select"]')
    expect((select.element as HTMLSelectElement).value).toBe('10')

    await select.setValue('20')
    await flushPromises()

    const saveButton = wrapper.get('[data-testid="save-children"]')
    expect(saveButton.attributes('disabled')).toBeUndefined()
    await saveButton.trigger('click')
    await flushPromises()

    const post = fetchMock.mock.calls.find(
      (call) =>
        (call[1] as RequestInit).method === 'POST' &&
        String(call[0]).endsWith('/children'),
    )
    expect(post).toBeDefined()
    expect(String(post![0])).toBe(
      '/api/v1/seasons/2026/duplications/g1/children',
    )
    expect(bodyOf(post as unknown[])).toEqual({ assignments: { c1: 20 } })
    expect(wrapper.get('[data-testid="decision-notice"]').text()).toContain(
      'Family assignments saved',
    )
  })

  it('lists the children attributed to each family entry in the member column', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail(
                [
                  member({
                    zite_record_id: 'f1',
                    survivor: true,
                    row_key: '10',
                    children: [
                      {
                        zite_record_id: 'c1',
                        first_name: 'Ann',
                        dob: '2018-01-01',
                        family_id: 10,
                        angel_id: '10A',
                      },
                    ],
                  }),
                  member({
                    zite_record_id: 'f2',
                    survivor: false,
                    decision: 'not_duplicate',
                    assigned_id: '20',
                    state: 'released',
                    row_key: '20',
                    children: [
                      {
                        zite_record_id: 'c2',
                        first_name: 'Anne',
                        dob: '2019-02-02',
                        family_id: 20,
                        angel_id: '20A',
                      },
                    ],
                  }),
                ],
                { entity: 'family', status: 'resolved' },
              ),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const f1Children = wrapper.get('th[data-member="f1"] [data-testid="member-children"]')
    expect(f1Children.text()).toContain('Children submitted with this entry')
    expect(f1Children.text()).toContain('Ann')
    expect(f1Children.text()).toContain('2018-01-01')

    const f2Children = wrapper.get('th[data-member="f2"] [data-testid="member-children"]')
    expect(f2Children.text()).toContain('Anne')
    expect(f2Children.text()).toContain('2019-02-02')
  })

  it('shows no per-member children block for a child group', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail([
                member(),
                member({ zite_record_id: 'r2', survivor: false }),
              ]),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="member-children"]').exists()).toBe(false)
  })

  it('names the entry each child was submitted with in the assignment table', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail(
                [
                  member({
                    zite_record_id: 'f1',
                    survivor: true,
                    row_key: '10',
                    children: [
                      {
                        zite_record_id: 'c1',
                        first_name: 'Ann',
                        dob: '2018-01-01',
                        family_id: 10,
                        angel_id: '10A',
                      },
                    ],
                  }),
                  member({
                    zite_record_id: 'f2',
                    survivor: false,
                    decision: 'not_duplicate',
                    assigned_id: '20',
                    state: 'released',
                    row_key: '20',
                    children: [
                      {
                        zite_record_id: 'c2',
                        first_name: 'Anne',
                        dob: '2019-02-02',
                        family_id: 20,
                        angel_id: '20A',
                      },
                    ],
                  }),
                ],
                {
                  entity: 'family',
                  status: 'resolved',
                  children: [
                    {
                      zite_record_id: 'c1',
                      first_name: 'Ann',
                      dob: '2018-01-01',
                      family_id: 10,
                      angel_id: '10A',
                    },
                    {
                      zite_record_id: 'c2',
                      first_name: 'Anne',
                      dob: '2019-02-02',
                      family_id: 20,
                      angel_id: '20A',
                    },
                  ],
                },
              ),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    expect(wrapper.get('tr[data-child="c1"]').text()).toContain('f1')
    expect(wrapper.get('tr[data-child="c2"]').text()).toContain('f2')
  })
})

describe('duplication kind', () => {
  it('renders the kind chips and sends the chosen kind in the request and URL', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications?')
          ? jsonResponse(200, { items: [], total: 0, page: 1, page_size: 50 })
          : undefined,
    ])
    const { wrapper, router } = await mountRouted(DuplicationListView, {
      path: '/audit/duplications',
      setup: seedSeason,
    })
    await flushPromises()

    const chips = () => wrapper.findAll('[data-testid="kind-chip"]')
    expect(chips().map((chip) => chip.text())).toEqual([
      'All',
      'Duplicate submissions',
      'Assigned Angel ID conflicts',
    ])
    expect(chips()[0]!.attributes('aria-pressed')).toBe('true')

    await chips()[2]!.trigger('click')
    await flushPromises()

    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain(
      'kind=angel_claim_conflict',
    )
    expect(router.currentRoute.value.query.kind).toBe('angel_claim_conflict')
    expect(chips()[2]!.attributes('aria-pressed')).toBe('true')
    expect(chips()[0]!.attributes('aria-pressed')).toBe('false')
  })

  it('labels each group with its kind and restores the kind filter from the URL', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications?')
          ? jsonResponse(200, {
              items: [
                GROUPS[0],
                {
                  ...GROUPS[1],
                  id: 'g3',
                  entity: 'sponsor',
                  kind: 'angel_claim_conflict',
                  group_key: '12A',
                },
              ],
              total: 2,
              page: 1,
              page_size: 50,
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationListView, {
      path: '/audit/duplications?kind=angel_claim_conflict',
      setup: seedSeason,
    })
    await flushPromises()

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      'kind=angel_claim_conflict',
    )
    const badges = wrapper.findAll('[data-testid="kind-badge"]')
    expect(badges.map((badge) => badge.text())).toEqual([
      'Duplicate submission',
      'Assigned Angel ID conflict',
    ])
    expect(
      wrapper.findAll('[data-testid="kind-chip"]')[2]!.attributes('aria-pressed'),
    ).toBe('true')
    expect(wrapper.find('input[name="q"]').attributes('placeholder')).toContain(
      'angel ID',
    )
  })

  it('shows the contested angel id and both decisions for an angel claim conflict', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail(
                [
                  member({
                    zite_record_id: 'walmart',
                    survivor: false,
                    raw: { first_name: 'Walmart', prior_assigned_angel_ids: '12A' },
                    screened: { zite_record_id: 'walmart', row_version: 1 },
                  }),
                  member({
                    zite_record_id: 'r2',
                    survivor: true,
                    raw: { first_name: 'Ann', prior_assigned_angel_ids: '12A' },
                    screened: null,
                  }),
                ],
                { entity: 'sponsor', kind: 'angel_claim_conflict', group_key: '12A' },
              ),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1?kind=angel_claim_conflict',
      setup: seedSeason,
    })
    await flushPromises()

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      'kind=angel_claim_conflict',
    )
    expect(wrapper.get('h1').text()).toBe('Angel ID 12A')
    expect(wrapper.get('[data-testid="contested-angel-id"]').text()).toContain('12A')
    const explainer = wrapper.get('[data-testid="contest-explainer"]').text()
    expect(explainer).toContain('Ignore')
    expect(explainer).toContain('settles the contest')
    expect(explainer).toContain('Not duplicate')
    expect(explainer).toContain('settles nothing')
    const claims = wrapper.findAll('[data-testid="member-angel-claim"]')
    expect(claims.map((claim) => claim.text())).toEqual([
      'Assigned Angel ID: 12A',
      'Assigned Angel ID: 12A',
    ])
  })
})

/** A saved entity response, as the entity PATCH returns it. */
function entityResponse(entity: string, row: Record<string, unknown>) {
  return jsonResponse(200, {
    entity,
    key: String(row.zite_record_id),
    row,
    baseline: {},
    submissions: [],
    status: 'new',
    row_version: 4,
    last_updated_by: 'cmendres',
    last_updated_at: '2026-11-20T10:00:00Z',
    audited_at: null,
    diagnostics: {},
  })
}

describe('duplication merged current column', () => {
  it('shows the merged values: the newest value wins and an older value fills a blank', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail(
                [
                  member({
                    zite_record_id: 'r1',
                    survivor: true,
                    raw: { first_name: 'Ann', dob: '' },
                    screened: { zite_record_id: 'r1', first_name: 'Old', dob: null, status: 'new' },
                  }),
                  member({
                    zite_record_id: 'r2',
                    survivor: false,
                    raw: { first_name: 'Ann', dob: '2019-02-02' },
                    screened: null,
                    row_version: null,
                  }),
                ],
                { current: { first_name: 'Ann', dob: '2019-02-02' }, current_row_key: 'r1' },
              ),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // The merged first name overrides the stored one; the stored dob is blank, so the older value fills it.
    expect(
      (wrapper.get('input[name="first_name"]').element as HTMLInputElement).value,
    ).toBe('Ann')
    expect(
      (wrapper.get('input[name="dob"]').element as HTMLInputElement).value,
    ).toBe('2019-02-02')
    expect(wrapper.get('[data-testid="current-explainer"]').text()).toContain(
      'newest submission',
    )
  })

  const sponsorMembers = (decisionOfS2: Record<string, unknown> = {}) => [
    member({
      zite_record_id: 's1',
      row_key: '10',
      survivor: true,
      raw: { first_name: 'Ann' },
      screened: { zite_record_id: 's1', sponsor_id: 10, first_name: 'Ann', status: 'new' },
      row_version: 3,
    }),
    member({
      zite_record_id: 's2',
      row_key: '11',
      survivor: false,
      raw: { first_name: 'Anne' },
      screened: { zite_record_id: 's2', sponsor_id: 11, first_name: 'Anne', status: 'new' },
      row_version: 4,
      ...decisionOfS2,
    }),
  ]
  const sponsorDetail = (
    members: unknown[],
    overrides: Record<string, unknown> = {},
  ) =>
    detail(members, {
      entity: 'sponsor',
      current_row_key: '10',
      current: { first_name: 'Ann' },
      ...overrides,
    })

  it('renders a sponsor number row with a select over the group numbers', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(200, sponsorDetail(sponsorMembers()))
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const row = wrapper.get('[data-field="sponsor_id"]')
    expect(row.get('td').text()).toBe('Sponsor number')
    expect(row.findAll('td[data-member]').map((cell) => cell.text())).toEqual([
      '10',
      '11',
    ])
    const select = wrapper.get('[data-testid="sponsor-pick"]')
    expect(select.attributes('aria-label')).toBe('Sponsor number')
    expect(
      select.findAll('option').map((option) => option.element.value),
    ).toEqual(['10', '11'])
    expect((select.element as HTMLSelectElement).value).toBe('10')
  })

  it('rebinds the column to the picked member row', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(200, sponsorDetail(sponsorMembers()))
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()
    // Both members are undecided, so the screen's flagged survivor is provisional.
    expect(wrapper.get('[data-testid="current-binding"]').text()).toBe(
      'Current column: provisional — the submission the screening run kept, s1.',
    )

    await wrapper.get('[data-testid="sponsor-pick"]').setValue('11')
    await flushPromises()

    expect(wrapper.get('[data-testid="current-binding"]').text()).toBe(
      'Current column: the merged entry is written to s2, not the surviving entry.',
    )
    expect(
      (wrapper.get('[data-testid="sponsor-pick"]').element as HTMLSelectElement).value,
    ).toBe('11')
  })

  it('sends the picked sponsor number with the save and the audit, but not with the entity patch', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'PATCH' && url.endsWith('/sponsors/11'))
          return entityResponse('sponsor', { zite_record_id: 's2', sponsor_id: 11, first_name: 'Annie', status: 'new' })
        if (init.method === 'POST' && url.endsWith('/audit'))
          return jsonResponse(200, sponsorDetail(sponsorMembers({ decision: 'not_duplicate', state: 'released' }), { status: 'audited' }))
        if (url.includes('/duplications/g1'))
          return jsonResponse(200, sponsorDetail(sponsorMembers({ decision: 'not_duplicate', state: 'released' })))
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    // s2 is decided, so s1 is the one undecided entry; pick s2's number and edit the column.
    await wrapper.get('[data-testid="sponsor-pick"]').setValue('11')
    await wrapper.get('input[name="first_name"]').setValue('Annie')
    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const patch = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'PATCH',
    )
    expect(String(patch![0])).toBe('/api/v1/seasons/2026/sponsors/11')
    expect(bodyOf(patch as unknown[])).toEqual({ first_name: 'Annie' })
    expect(bodyOf(patch as unknown[])).not.toHaveProperty('sponsor_id')

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()
    const audit = fetchMock.mock.calls.find(
      (call) =>
        (call[1] as RequestInit).method === 'POST' &&
        String(call[0]).endsWith('/audit'),
    )
    expect(bodyOf(audit as unknown[]).values).toHaveProperty('sponsor_id', 11)
    expect(bodyOf(audit as unknown[]).expected_version).toBe(4)
  })

  it('disables an ignored member’s number and says why', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              sponsorDetail(sponsorMembers({ decision: 'ignore', state: 'discarded' })),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const options = wrapper.findAll('[data-testid="sponsor-pick"] option')
    expect(options[0]!.attributes('disabled')).toBeUndefined()
    expect(options[1]!.attributes('disabled')).toBeDefined()
    expect(options[1]!.attributes('title')).toBe(
      'ignored — un-ignore that row on its own row first',
    )
    expect(wrapper.get('[data-testid="sponsor-ignored-hint"]').text()).toContain(
      'un-ignore that row on its own row first',
    )
  })

  it('marks a field the auditor changed as differs, and leaves untouched fields unmarked', async () => {
    mockRoutes([
      (url) =>
        url.includes('/duplications/g1')
          ? jsonResponse(
              200,
              detail(
                [
                  member({
                    zite_record_id: 'r1',
                    survivor: true,
                    raw: { first_name: 'Ann', dob: '2018-01-01' },
                    screened: { zite_record_id: 'r1', first_name: 'Ann', dob: '2018-01-01', status: 'new' },
                  }),
                  member({
                    zite_record_id: 'r2',
                    survivor: false,
                    raw: { first_name: 'Ann', dob: '' },
                    screened: null,
                    row_version: null,
                  }),
                ],
                { current: { first_name: 'Ann', dob: '2018-01-01' }, current_row_key: 'r1' },
              ),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    expect(wrapper.find('[data-field="first_name"] [data-testid="diff-marker"]').exists()).toBe(false)
    await wrapper.get('input[name="first_name"]').setValue('Annie')
    expect(wrapper.find('[data-field="first_name"] [data-testid="diff-marker"]').exists()).toBe(true)
    expect(wrapper.find('[data-field="dob"] [data-testid="diff-marker"]').exists()).toBe(false)
  })

  it('enables Save current values when the merged values differ from the stored row, with no edit', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (init.method === 'PATCH' && url.includes('/children/r1'))
          return entityResponse('child', { zite_record_id: 'r1', first_name: 'Anne', dob: '2018-01-01', status: 'new' })
        if (url.includes('/duplications/g1'))
          return jsonResponse(
            200,
            detail(
              [
                member({
                  zite_record_id: 'r1',
                  survivor: true,
                  raw: { first_name: 'Anne', dob: '2018-01-01' },
                  screened: { zite_record_id: 'r1', first_name: 'Ann', dob: '2018-01-01', status: 'new' },
                }),
              ],
              { current: { first_name: 'Anne' }, current_row_key: 'r1' },
            ),
          )
        return undefined
      },
    ])
    const { wrapper } = await mountRouted(DuplicationDetailView, {
      path: '/audit/duplications/g1',
      setup: seedSeason,
    })
    await flushPromises()

    const save = wrapper.get('[data-testid="save"]')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flushPromises()

    const patch = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit).method === 'PATCH',
    )
    expect(bodyOf(patch as unknown[])).toEqual({ first_name: 'Anne', dob: '2018-01-01' })
  })
})
