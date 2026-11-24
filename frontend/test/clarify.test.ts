import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import ClarifyListView from '../src/views/ClarifyListView.vue'
import ClarifyDetailView from '../src/views/ClarifyDetailView.vue'
import { useJobPopup } from '../src/composables/useJobPopup'
import { useSessionStore } from '../src/stores/session'
import {
  bodyOf,
  headersOf,
  jsonResponse,
  mockRoutes,
  mountRouted,
} from './helpers'

function family(overrides: Record<string, unknown> = {}) {
  return {
    family_id: 1,
    contacted: false,
    replied: false,
    updated_at: '2026-11-19T10:00:00Z',
    children: [
      {
        zite_record_id: 'c1',
        angel_id: '1A',
        first_name: 'Kid',
        raw_dob: '01/01/2015',
        clarification_notes: 'need shoe size',
        need_1: 'shoes',
        desire_1: 'book',
        family_email: 'fam@example.com',
        family_phone: '555-0100',
      },
    ],
    draft: {
      id: 1,
      family_id: 1,
      version: 2,
      subject: 'Gift details',
      greeting: 'Dear family,',
      intro: 'We need a little more info.',
      closing: 'Thank you',
      paragraphs: [
        { zite_record_id: 'c1', angel_id: '1A', order: 0, text: 'Para one' },
      ],
      legacy_positional: false,
      generated_by: 'system',
      edited_by: 'cmendres',
      edited_at: '2026-11-19T09:00:00Z',
    },
    ...overrides,
  }
}

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

beforeEach(() => {
  setActivePinia(createPinia())
  useJobPopup().closeJob()
  expect(useJobPopup().jobId.value).toBe(null)
})

describe('clarify list', () => {
  it('lists flagged families and shows a blank contact explicitly', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/clarifications')
          ? jsonResponse(200, [
              family(),
              family({
                family_id: 2,
                children: [
                  {
                    zite_record_id: 'c2',
                    family_email: '',
                    family_phone: '',
                  },
                ],
                draft: null,
              }),
            ])
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyListView, {
      path: '/clarify',
      setup: withSeason,
    })
    await flushPromises()

    const rows = wrapper.get('[data-testid="clarify-list"]').findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('fam@example.com')
    expect(rows[0]!.text()).toContain('v2')
    expect(rows[1]!.text()).toContain('no contact on file')
  })

  it('shows the empty state when nothing is flagged', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/clarifications')
          ? jsonResponse(200, [])
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyListView, {
      path: '/clarify',
      setup: withSeason,
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="clarify-empty"]').exists()).toBe(true)
  })
})

describe('clarify detail', () => {
  it('renders children and the draft as escaped text only', async () => {
    mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/clarifications/1')
          ? jsonResponse(
              200,
              family({
                draft: {
                  ...family().draft,
                  subject: '<script>alert(1)</script>',
                },
              }),
            )
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="clarify-children"]').text()).toContain(
      'need shoe size',
    )
    const preview = wrapper.get('[data-testid="draft-preview"]')
    expect(preview.text()).toContain('<script>alert(1)</script>')
    expect(preview.find('script').exists()).toBe(false)
    expect(preview.html()).toContain('&lt;script&gt;')
  })

  it('saves only touched fields with If-Match = draft version', async () => {
    const updated = {
      ...family().draft,
      version: 3,
      subject: 'New subject',
    }
    const fetchMock = mockRoutes([
      (url, init) =>
        url.endsWith('/clarifications/1/draft') &&
        String(init.method) === 'PATCH'
          ? jsonResponse(200, updated)
          : undefined,
      (url) =>
        url.endsWith('/seasons/2026/clarifications/1')
          ? jsonResponse(200, family())
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="edit-draft"]').trigger('click')
    const subject = wrapper.get('input[name="subject"]')
    await subject.setValue('New subject')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const call = fetchMock.mock.calls.find((entry) =>
      String(entry[0]).endsWith('/clarifications/1/draft'),
    )
    expect(call).toBeDefined()
    expect(bodyOf(call!)).toEqual({ subject: 'New subject' })
    expect(headersOf(call!)['If-Match']).toBe('2')
  })

  it('shows the conflict panel on a stale save and keeps local edits', async () => {
    let detailCalls = 0
    const fresh = family({
      draft: {
        ...family().draft,
        version: 3,
        subject: 'Server subject',
      },
    })
    const fetchMock = mockRoutes([
      (url, init) =>
        url.endsWith('/clarifications/1/draft') &&
        String(init.method) === 'PATCH'
          ? jsonResponse(412, {
              detail: {
                code: 'stale_version',
                message: 'family 1 draft is at version 3, not 2',
              },
            })
          : undefined,
      (url) => {
        if (!url.endsWith('/seasons/2026/clarifications/1')) return undefined
        detailCalls += 1
        return jsonResponse(200, detailCalls === 1 ? family() : fresh)
      },
    ])
    const { wrapper } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="edit-draft"]').trigger('click')
    await wrapper.get('input[name="subject"]').setValue('Mine')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[data-testid="conflict-panel"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="conflict-panel"]').text()).toContain(
      'Server subject',
    )
    expect(
      (wrapper.get('input[name="subject"]').element as HTMLInputElement).value,
    ).toBe('Mine')

    // Rebase then save again: the retry carries the fresh version.
    await wrapper.get('[data-testid="conflict-rebase"]').trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const patches = fetchMock.mock.calls.filter((entry) =>
      String(entry[0]).endsWith('/clarifications/1/draft'),
    )
    expect(headersOf(patches.at(-1)!)['If-Match']).toBe('3')
  })

  it('toggles contacted and replied through the API', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.endsWith('/clarifications/1/contacted')
          ? jsonResponse(200, { ...family(), contacted: true, contacted_at: '2026-11-19T11:00:00Z' })
          : undefined,
      (url) =>
        url.endsWith('/clarifications/1/replied')
          ? jsonResponse(200, { ...family(), replied: true })
          : undefined,
      (url) =>
        url.endsWith('/seasons/2026/clarifications/1')
          ? jsonResponse(200, family())
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="toggle-contacted"]').trigger('click')
    await flushPromises()
    const contacted = fetchMock.mock.calls.find((entry) =>
      String(entry[0]).endsWith('/contacted'),
    )
    expect(bodyOf(contacted!)).toEqual({ contacted: true })
    expect(wrapper.get('[data-testid="toggle-contacted"]').text()).toContain(
      'yes',
    )

    await wrapper.get('[data-testid="toggle-replied"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="toggle-replied"]').text()).toContain(
      'yes',
    )
  })

  it('enqueues the CSV export and opens the job', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/clarify-export')
          ? jsonResponse(202, {
              id: 31,
              job_type: 'clarify_export',
              status: 'queued',
              requester: 'lmboswell',
              attempt: 0,
              cancel_requested: false,
            })
          : undefined,
      (url) =>
        url.endsWith('/seasons/2026/clarifications/1')
          ? jsonResponse(200, family())
          : undefined,
    ])
    const { wrapper, router } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="clarify-export"]').trigger('click')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    expect(
      fetchMock.mock.calls.some((entry) =>
        String(entry[0]).endsWith('/seasons/2026/clarify-export'),
      ),
    ).toBe(true)
    expect(useJobPopup().jobId.value).toBe(31)
    expect(router.currentRoute.value.path).toBe('/clarify/1')
  })

  it('enqueues a draft regeneration job with confirm', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.endsWith('/seasons/2026/jobs')
          ? jsonResponse(202, {
              id: 32,
              job_type: 'clarify_generate',
              status: 'queued',
              requester: 'lmboswell',
              attempt: 0,
              cancel_requested: false,
            })
          : undefined,
      (url) =>
        url.endsWith('/seasons/2026/clarifications/1')
          ? jsonResponse(200, family())
          : undefined,
    ])
    const { wrapper } = await mountRouted(ClarifyDetailView, {
      path: '/clarify/1',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="regenerate-draft"]').trigger('click')
    expect(fetchMock.mock.calls).toHaveLength(1) // only the detail GET
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const call = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/seasons/2026/jobs') &&
        String((entry[1] as RequestInit).method) === 'POST',
    )
    expect(bodyOf(call!)).toEqual({
      job_type: 'clarify_generate',
      options: { overwrite: true },
    })
  })
})
