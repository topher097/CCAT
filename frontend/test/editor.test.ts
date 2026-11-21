import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

import EntityEditor from '../src/components/EntityEditor.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, headersOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const CHILD_ROW = {
  zite_record_id: 'c1',
  family_id: 1,
  first_name: 'Ann',
  primary_email: 'ann@example.org',
  dob: '2018-01-01',
  age_reported: 7,
  age_at_xmas: 8,
  gender: 'F',
  special_needs: 'ADHD',
  ignore: false,
  status: 'new',
  clarification_notes: null,
  ai_screening_notes: 'possible duplicate',
}

const CHILD_BASELINE = {
  first_name: 'Ann',
  primary_email: 'ann@example.org',
  dob: '2018-01-01',
  age_reported: 7,
  gender: 'F',
  special_needs: null,
  clarification_notes: null,
  ai_screening_notes: 'possible duplicate',
}

function childDetail(overrides: Record<string, unknown> = {}) {
  return {
    entity: 'child',
    key: 'c1',
    row: CHILD_ROW,
    baseline: CHILD_BASELINE,
    submissions: [],
    status: 'new',
    row_version: 3,
    last_updated_by: 'cmendres',
    last_updated_at: '2026-11-18T10:00:00Z',
    audited_at: null,
    diagnostics: {},
    ...overrides,
  }
}

async function mountEditor(entity: 'child' | 'sponsor' | 'family', key = 'c1') {
  const { wrapper } = await mountRouted(EntityEditor, {
    path: `/audit/${entity === 'child' ? 'children' : entity === 'family' ? 'families' : 'sponsors'}/${key}`,
    props: { entity, entityKey: key },
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

function patchCall(fetchMock: ReturnType<typeof mockRoutes>) {
  return fetchMock.mock.calls.find(
    (call) => (call[1] as RequestInit | undefined)?.method === 'PATCH',
  )
}

beforeEach(() => setActivePinia(createPinia()))

describe('entity editor', () => {
  it('loads a child with baseline, current values and provenance', async () => {
    mockRoutes([
      (url) => (url.includes('/children/c1') ? jsonResponse(200, childDetail()) : undefined),
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    expect(wrapper.get('[data-field="first_name"]').text()).toContain('Ann')
    expect(wrapper.text()).toContain('Last updated by')
    expect(wrapper.text()).toContain('cmendres')
    expect(wrapper.text()).toContain('row version 3')
    expect(wrapper.get('[data-testid="age-warning"]').text()).toContain(
      'Age mismatch',
    )
  })

  it('sends only touched fields (tri-state) with If-Match', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(
            200,
            childDetail({
              row: { ...CHILD_ROW, first_name: 'Anna', primary_email: null },
              row_version: 4,
            }),
          )
        }
        return jsonResponse(200, childDetail())
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('input[name="first_name"]').setValue('Anna')
    await wrapper.get('input[name="primary_email"]').setValue('')
    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const call = patchCall(fetchMock)
    expect(call).toBeDefined()
    expect(bodyOf(call!)).toEqual({
      first_name: 'Anna',
      primary_email: null,
    })
    expect(headersOf(call!)['If-Match']).toBe('3')
    expect(wrapper.get('[data-field="first_name"]').classes()).toContain(
      'diff-changed',
    )
    expect(wrapper.get('[data-testid="save-notice"]').text()).toContain('Saved')
  })

  it('shows special-needs diffs neutrally and serializes the control', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(
            200,
            childDetail({
              row: { ...CHILD_ROW, special_needs: 'ADHD; Autism' },
              row_version: 4,
            }),
          )
        }
        return jsonResponse(200, childDetail())
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('input[name="special-needs-Autism"]').setValue(true)

    const row = wrapper.get('[data-field="special_needs"]')
    expect(row.classes()).toContain('diff-neutral')
    expect(row.text()).toContain('not status-relevant')

    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()
    expect(bodyOf(patchCall(fetchMock)!)).toEqual({
      special_needs: 'ADHD; Autism',
    })
  })

  it('keeps edits on 412, shows the winner, and rebases onto the fresh row', async () => {
    let patches = 0
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          patches += 1
          if (patches === 1) {
            return jsonResponse(412, {
              detail: {
                code: 'stale_version',
                message: 'row version changed',
                entity: 'child',
                key: 'c1',
                current_version: 4,
                row: { ...CHILD_ROW, first_name: 'Bob' },
                last_updated_by: 'lmboswell',
                last_updated_at: '2026-11-18T11:00:00Z',
              },
            })
          }
          return jsonResponse(
            200,
            childDetail({
              row: { ...CHILD_ROW, first_name: 'Anna' },
              row_version: 5,
            }),
          )
        }
        return jsonResponse(200, childDetail())
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('input[name="first_name"]').setValue('Anna')
    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()

    const panel = wrapper.get('[data-testid="conflict-panel"]')
    expect(panel.text()).toContain('lmboswell')
    expect(panel.text()).toContain('Bob')
    expect(
      (wrapper.get('input[name="first_name"]').element as HTMLInputElement)
        .value,
    ).toBe('Anna')

    await wrapper.get('[data-testid="conflict-rebase"]').trigger('click')
    await flushPromises()

    const calls = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit).method === 'PATCH',
    )
    expect(calls).toHaveLength(2)
    expect(headersOf(calls[1]!)['If-Match']).toBe('4')
    expect(bodyOf(calls[1]!)).toEqual({ first_name: 'Anna' })
    expect(
      (wrapper.get('input[name="first_name"]').element as HTMLInputElement)
        .value,
    ).toBe('Anna')
    expect(wrapper.find('[data-testid="conflict-panel"]').exists()).toBe(false)
  })

  it('reload discards local edits and shows the fresh server row', async () => {
    mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(412, {
            detail: {
              code: 'stale_version',
              message: 'row version changed',
              entity: 'child',
              key: 'c1',
              current_version: 4,
              row: { ...CHILD_ROW, first_name: 'Bob' },
              last_updated_by: 'lmboswell',
              last_updated_at: '2026-11-18T11:00:00Z',
            },
          })
        }
        return jsonResponse(
          200,
          childDetail({ row: { ...CHILD_ROW, first_name: 'Bob' }, row_version: 4 }),
        )
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('input[name="first_name"]').setValue('Anna')
    await wrapper.get('[data-testid="save"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="conflict-reload"]').trigger('click')
    await flushPromises()

    expect(
      (wrapper.get('input[name="first_name"]').element as HTMLInputElement)
        .value,
    ).toBe('Bob')
    expect(wrapper.find('[data-testid="conflict-panel"]').exists()).toBe(false)
  })

  it('warns that an orphan child cannot be audited', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1')
          ? jsonResponse(
              200,
              childDetail({
                row: { ...CHILD_ROW, family_id: null, age_reported: null },
              }),
            )
          : undefined,
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()
    expect(wrapper.get('[data-testid="orphan-warning"]').text()).toContain(
      'Audited status stays disabled',
    )
    expect(
      (wrapper.get('[data-testid="mark-audited"]').element as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('shows the angel id, the family child count and the family link', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1')
          ? jsonResponse(
              200,
              childDetail({
                row: {
                  ...CHILD_ROW,
                  angel_id: '1A',
                  family_child_count: 2,
                },
              }),
            )
          : undefined,
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    const block = wrapper.get('[data-testid="child-identity"]')
    expect(block.text()).toContain('Angel ID:')
    expect(block.text()).toContain('1A')
    expect(block.text()).toContain('Children in family: 2')
    const link = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .find((candidate) => String(candidate.props('to')).startsWith('/audit/families/'))
    expect(link!.props('to')).toBe('/audit/families/1')
  })

  it('blocks auditing a row whose duplicate group still awaits audit', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1')
          ? jsonResponse(
              200,
              childDetail({
                row: {
                  ...CHILD_ROW,
                  duplicate_pending: true,
                  duplicate_group_id: 'g7',
                },
              }),
            )
          : undefined,
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    expect(
      wrapper.get('[data-testid="duplicate-pending-warning"]').text(),
    ).toContain('has not been audited')
    const link = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .find((candidate) => String(candidate.props('to')).startsWith('/audit/duplications/'))
    expect(link!.props('to')).toBe('/audit/duplications/g7')
    expect(
      (wrapper.get('[data-testid="mark-audited"]').element as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('names the duplicate group when the server refuses to audit', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(409, {
            detail: {
              code: 'duplicate_pending',
              message: 'duplicate group not audited',
            },
          })
        }
        return jsonResponse(
          200,
          childDetail({ row: { ...CHILD_ROW, duplicate_group_id: 'g7' } }),
        )
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    expect(patchCall(fetchMock)).toBeDefined()
    expect(wrapper.get('[data-testid="save-error"]').text()).toContain(
      'duplicate group g7',
    )
    expect(wrapper.find('[data-testid="conflict-panel"]').exists()).toBe(false)
  })

  it('marks the row audited together with the pending edits', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(
            200,
            childDetail({
              row: { ...CHILD_ROW, first_name: 'Anna', status: 'audited' },
              status: 'audited',
              row_version: 4,
            }),
          )
        }
        return jsonResponse(200, childDetail())
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('input[name="first_name"]').setValue('Anna')
    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    const call = patchCall(fetchMock)
    expect(bodyOf(call!)).toEqual({ first_name: 'Anna', status: 'audited' })
    expect(headersOf(call!)['If-Match']).toBe('3')
    expect(wrapper.get('[data-testid="mark-audited"]').text()).toContain(
      'Undo audit',
    )
    expect(wrapper.get('[data-testid="save-notice"]').text()).toContain(
      'Marked audited',
    )
  })

  it('undoes an audit', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/children/c1')) return undefined
        if (init.method === 'PATCH') {
          return jsonResponse(
            200,
            childDetail({
              row: { ...CHILD_ROW, status: 'new' },
              status: 'new',
              row_version: 4,
            }),
          )
        }
        return jsonResponse(
          200,
          childDetail({
            row: { ...CHILD_ROW, status: 'audited' },
            status: 'audited',
          }),
        )
      },
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()

    await wrapper.get('[data-testid="mark-audited"]').trigger('click')
    await flushPromises()

    expect(bodyOf(patchCall(fetchMock)!)).toEqual({ status: 'new' })
    expect(wrapper.get('[data-testid="mark-audited"]').text()).toContain(
      'Mark audited',
    )
  })

  it('lists duplicate submissions', async () => {
    mockRoutes([
      (url) =>
        url.includes('/children/c1')
          ? jsonResponse(
              200,
              childDetail({
                submissions: [
                  {
                    zite_record_id: 'c1',
                    first_name: 'Ann',
                    primary_email: 'ann@example.org',
                    dob: '2018-01-01',
                  },
                  {
                    zite_record_id: 'c9',
                    first_name: 'Ann',
                    primary_email: 'ann@example.org',
                    dob: '2018-01-01',
                  },
                ],
              }),
            )
          : undefined,
    ])
    const wrapper = await mountEditor('child')
    await flushPromises()
    expect(wrapper.get('[data-testid="submissions"]').findAll('tbody tr')).toHaveLength(2)
  })
})

const SPONSOR_ROW = {
  sponsor_id: 10,
  first_name: 'Ada',
  last_name: 'Lovelace',
  email: 'ada@example.org',
  status: 'new',
  ignore: false,
  clarification_notes: null,
}

function sponsorDetail(overrides: Record<string, unknown> = {}) {
  return {
    entity: 'sponsor',
    key: '10',
    row: SPONSOR_ROW,
    baseline: { first_name: 'Ada', email: 'ada@example.org' },
    submissions: [],
    status: 'new',
    row_version: 3,
    last_updated_by: 'cmendres',
    last_updated_at: '2026-11-18T10:00:00Z',
    audited_at: null,
    diagnostics: {},
    ...overrides,
  }
}

describe('ignore / unignore', () => {
  it('shows the email and address validation status', async () => {
    mockRoutes([
      (url) =>
        url.includes('/sponsors/10')
          ? jsonResponse(
              200,
              sponsorDetail({
                diagnostics: { email_status: 'valid', address_status: null },
              }),
            )
          : undefined,
    ])
    const wrapper = await mountEditor('sponsor', '10')
    await flushPromises()

    const panel = wrapper.get('[data-testid="validation-status"]')
    expect(panel.text()).toContain('email: valid')
    expect(panel.text()).not.toContain('address:')
  })

  it('requires an explicit destructive confirmation and sends expected_version', async () => {
    const fetchMock = mockRoutes([
      (url, init) => {
        if (!url.includes('/sponsors/10')) return undefined
        if (init.method === 'POST' && url.endsWith('/ignore')) {
          return jsonResponse(
            200,
            sponsorDetail({
              row: { ...SPONSOR_ROW, ignore: true, status: 'ignored' },
              row_version: 4,
            }),
          )
        }
        if (init.method === 'POST' && url.endsWith('/unignore')) {
          return jsonResponse(
            200,
            sponsorDetail({ row: { ...SPONSOR_ROW }, row_version: 5 }),
          )
        }
        return jsonResponse(200, sponsorDetail())
      },
    ])
    const wrapper = await mountEditor('sponsor', '10')
    await flushPromises()

    await wrapper.get('[data-testid="ignore-toggle"]').trigger('click')
    expect(wrapper.get('[role="dialog"]').text()).toContain(
      'destroys this row',
    )
    // Cancel does not call the API.
    await wrapper
      .findAll('[role="dialog"] button')
      .find((button) => button.text() === 'Cancel')!
      .trigger('click')
    expect(
      fetchMock.mock.calls.filter(
        (call) => (call[1] as RequestInit).method === 'POST',
      ),
    ).toHaveLength(0)

    await wrapper.get('[data-testid="ignore-toggle"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const ignoreCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).endsWith('/ignore'),
    )
    expect(bodyOf(ignoreCall!)).toEqual({
      confirm: true,
      expected_version: 3,
    })
    expect(wrapper.get('[data-testid="ignore-toggle"]').text()).toContain(
      'Un-ignore',
    )
    expect(wrapper.get('[data-testid="ignore-toggle"]').text()).toContain(
      'restores raw values',
    )

    await wrapper.get('[data-testid="ignore-toggle"]').trigger('click')
    expect(wrapper.get('[role="dialog"]').text()).toContain(
      'previous adjudicated edits',
    )
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const unignoreCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).endsWith('/unignore'),
    )
    expect(bodyOf(unignoreCall!)).toEqual({
      confirm: true,
      expected_version: 4,
    })
  })
})
