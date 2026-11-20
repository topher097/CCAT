import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import StatsView from '../src/views/StatsView.vue'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

beforeEach(() => setActivePinia(createPinia()))

describe('stats reports', () => {
  it('enqueues a prepared-source stats job', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.includes('/seasons/2026/artifacts') ? jsonResponse(200, []) : undefined,
      (url) =>
        url.endsWith('/seasons/2026/jobs')
          ? jsonResponse(202, {
              id: 12,
              job_type: 'stats',
              status: 'queued',
              requester: 'lmboswell',
              attempt: 0,
              cancel_requested: false,
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(StatsView, {
      path: '/stats',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('input[name="prepared"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const call = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/seasons/2026/jobs') &&
        String((entry[1] as RequestInit).method) === 'POST',
    )
    expect(call).toBeDefined()
    expect(bodyOf(call!)).toEqual({
      job_type: 'stats',
      options: { raw: true },
    })
  })

  it('lists HTML/PDF reports with authorized download links', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/artifacts') && url.includes('kind=stats_report')
          ? jsonResponse(200, [
              {
                id: 20,
                kind: 'stats_report',
                filename: 'stats.html',
                meta: { complete: true },
                created_at: '2026-11-19T10:00:00Z',
              },
              {
                id: 21,
                kind: 'stats_report',
                filename: 'stats.pdf',
                meta: { complete: true },
                created_at: '2026-11-19T10:00:00Z',
              },
            ])
          : undefined,
    ])
    const { wrapper } = await mountRouted(StatsView, {
      path: '/stats',
      setup: withSeason,
    })
    await flushPromises()

    const rows = wrapper
      .get('[data-testid="stats-artifacts"]')
      .findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('HTML')
    expect(rows[1]!.text()).toContain('PDF')
    const hrefs = wrapper
      .findAll('[data-testid="artifact-download"]')
      .map((link) => link.attributes('href'))
    expect(hrefs).toEqual([
      '/api/v1/artifacts/20/download',
      '/api/v1/artifacts/21/download',
    ])
  })

  it('flags an HTML-only report as an incomplete report', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/artifacts')
          ? jsonResponse(200, [
              {
                id: 20,
                kind: 'stats_report',
                filename: 'stats.html',
                meta: { complete: false, mode: 'final' },
                created_at: '2026-11-19T10:00:00Z',
              },
            ])
          : undefined,
    ])
    const { wrapper } = await mountRouted(StatsView, {
      path: '/stats',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="stats-incomplete"]').text()).toContain(
      'incomplete report',
    )
  })
})
