import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import GenerateView from '../src/views/GenerateView.vue'
import { useSessionStore } from '../src/stores/session'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

const ARTIFACTS = [
  {
    id: 3,
    kind: 'child_sheet',
    filename: '1A.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    meta: { zite_record_id: 'c1', angel_id: '1A' },
    created_at: '2026-11-19T10:00:00Z',
  },
  {
    id: 4,
    kind: 'sponsor_packet',
    filename: 'mary.pdf',
    content_type: 'application/pdf',
    size_bytes: 4096,
    meta: {},
    created_at: '2026-11-19T10:05:00Z',
  },
]

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

function childPage() {
  return jsonResponse(200, {
    items: [
      {
        zite_record_id: 'c1',
        first_name: 'Kid',
        family_id: 1,
        generated_info_sheet: true,
      },
      {
        zite_record_id: 'c2',
        first_name: 'Other',
        family_id: 1,
        generated_info_sheet: false,
      },
    ],
    total: 2,
    page: 1,
    page_size: 200,
  })
}

beforeEach(() => setActivePinia(createPinia()))

describe('generate artifacts', () => {
  it('lists artifacts with downloads through the authorized route', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/artifacts') && !url.includes('kind=')
          ? jsonResponse(200, ARTIFACTS)
          : undefined,
      (url) =>
        url.includes('/seasons/2026/children?') ? childPage() : undefined,
    ])
    const { wrapper } = await mountRouted(GenerateView, {
      path: '/generate',
      setup: withSeason,
    })
    await flushPromises()

    const rows = wrapper.get('[data-testid="artifact-list"]').findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('c1')

    const hrefs = wrapper
      .findAll('[data-testid="artifact-download"]')
      .map((link) => link.attributes('href'))
    expect(hrefs).toEqual([
      '/api/v1/artifacts/3/download',
      '/api/v1/artifacts/4/download',
    ])
    expect(hrefs.every((href) => !String(href).includes('..'))).toBe(true)
  })

  it('passes the kind filter to the artifact request', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/seasons/2026/artifacts') && url.includes('kind=sponsor_packet')
          ? jsonResponse(200, [ARTIFACTS[1]])
          : undefined,
      (url) =>
        url.includes('/seasons/2026/artifacts')
          ? jsonResponse(200, ARTIFACTS)
          : undefined,
      (url) =>
        url.includes('/seasons/2026/children?') ? childPage() : undefined,
    ])
    const { wrapper } = await mountRouted(GenerateView, {
      path: '/generate',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('select[aria-label="Artifact kind filter"]').setValue(
      'sponsor_packet',
    )
    await flushPromises()

    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes('kind=sponsor_packet'),
      ),
    ).toBe(true)
    expect(
      wrapper.get('[data-testid="artifact-list"]').findAll('tbody tr'),
    ).toHaveLength(1)
  })

  it('shows per-child generation markers as text badges', async () => {
    mockRoutes([
      (url) => (url.includes('/seasons/2026/artifacts') ? jsonResponse(200, []) : undefined),
      (url) =>
        url.includes('/seasons/2026/children?') ? childPage() : undefined,
    ])
    const { wrapper } = await mountRouted(GenerateView, {
      path: '/generate',
      setup: withSeason,
    })
    await flushPromises()

    const rows = wrapper
      .get('[data-testid="child-markers"]')
      .findAll('tbody tr')
    expect(rows[0]!.text()).toContain('sheet generated')
    expect(rows[1]!.text()).toContain('pending')
  })
})
