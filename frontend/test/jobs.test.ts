import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import JobsListView from '../src/views/JobsListView.vue'
import JobDetailView from '../src/views/JobDetailView.vue'
import JobPopup from '../src/components/JobPopup.vue'
import { useJobPopup } from '../src/composables/useJobPopup'
import { useSessionStore } from '../src/stores/session'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

const JOB = {
  id: 5,
  job_type: 'screen',
  status: 'running',
  requester: 'lmboswell',
  attempt: 0,
  cancel_requested: false,
  progress: { phase: 'ai', completed: 2, failed: 0, total: 10 },
  created_at: '2026-11-19T10:00:00Z',
  events: [{ id: 1, kind: 'started', ts: '2026-11-19T10:00:01Z', data: {} }],
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>)
  }

  fail(): void {
    this.onerror?.()
  }
}

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

function detailRoutes(overrides: Record<string, unknown> = {}) {
  return mockRoutes([
    (url) =>
      url.endsWith('/api/v1/jobs/5')
        ? jsonResponse(200, { ...JOB, ...overrides })
        : undefined,
  ])
}

beforeEach(() => {
  setActivePinia(createPinia())
  useJobPopup().closeJob()
  expect(useJobPopup().jobId.value).toBe(null)
  FakeEventSource.instances = []
  vi.useRealTimers()
})

describe('job popup', () => {
  it('shows the selected job and closes from the overlay', async () => {
    useJobPopup().openJob(17)
    const wrapper = mount(JobPopup)

    expect(wrapper.get('iframe').attributes('src')).toBe('/jobs/17?embed=1')
    expect(wrapper.get('iframe').attributes('title')).toBe('Job #17')

    await wrapper.get('[data-testid="popup-frame-close"]').trigger('click')
    expect(useJobPopup().jobId.value).toBe(null)
    expect(wrapper.find('.dialog-backdrop').exists()).toBe(false)
  })
})

describe('job center list', () => {
  it('renders jobs with status and progress summaries', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/jobs')
          ? jsonResponse(200, [
              JOB,
              {
                ...JOB,
                id: 4,
                job_type: 'generate',
                status: 'succeeded',
                progress: { phase: 'packets', completed: 3, failed: 0, total: 3 },
              },
            ])
          : undefined,
    ])
    const { wrapper } = await mountRouted(JobsListView, {
      path: '/jobs',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="jobs-total"]').text()).toBe('2 jobs')
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('screen')
    expect(rows[0]!.text()).toContain('running')
    expect(rows[0]!.text()).toContain('ai')
    expect(rows[1]!.text()).toContain('generate')
    expect(rows[1]!.text()).toContain('succeeded')
  })

  it('filters by status and type client-side', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/jobs')
          ? jsonResponse(200, [
              JOB,
              { ...JOB, id: 4, job_type: 'generate', status: 'failed' },
            ])
          : undefined,
    ])
    const { wrapper } = await mountRouted(JobsListView, {
      path: '/jobs',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper
      .get('select[aria-label="Filter by job status"]')
      .setValue('failed')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.get('[data-testid="jobs-total"]').text()).toBe('1 jobs')

    await wrapper
      .get('select[aria-label="Filter by job status"]')
      .setValue('all')
    await wrapper.get('select[aria-label="Filter by job type"]').setValue('screen')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.findAll('tbody tr')[0]!.text()).toContain('screen')
  })

  it('shows an empty state when no job matches', async () => {
    mockRoutes([
      (url) =>
        url.includes('/seasons/2026/jobs')
          ? jsonResponse(200, [])
          : undefined,
    ])
    const { wrapper } = await mountRouted(JobsListView, {
      path: '/jobs',
      setup: withSeason,
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="jobs-empty"]').exists()).toBe(true)
  })
})

describe('job detail', () => {
  it('updates live progress from the 2s poll', async () => {
    vi.useFakeTimers()
    let calls = 0
    mockRoutes([
      (url) => {
        if (!url.endsWith('/api/v1/jobs/5')) return undefined
        calls += 1
        return jsonResponse(200, {
          ...JOB,
          progress:
            calls === 1
              ? { phase: 'ai', completed: 1, failed: 0, total: 10 }
              : { phase: 'ai', completed: 7, failed: 0, total: 10 },
        })
      },
    ])
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="job-progress"]').text()).toContain('1/10')

    await vi.advanceTimersByTimeAsync(2100)
    await flushPromises()
    expect(wrapper.get('[data-testid="job-progress"]').text()).toContain('7/10')
  })

  it('connects SSE, applies progress events, and reconnects after errors', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    detailRoutes()
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    expect(FakeEventSource.instances).toHaveLength(1)
    const first = FakeEventSource.instances[0]!
    expect(first.url).toBe('/api/v1/jobs/5/events/stream')

    first.emit({
      kind: 'progress',
      data: { phase: 'ai', completed: 5, failed: 0, total: 10 },
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="job-progress"]').text()).toContain('5/10')

    first.fail()
    await vi.advanceTimersByTimeAsync(3100)
    expect(FakeEventSource.instances).toHaveLength(2)
  })

  it('requires confirmation and posts the cancel request', async () => {
    const fetchMock = mockRoutes([
      (url) =>
        url.endsWith('/api/v1/jobs/5') && !url.includes('/cancel')
          ? jsonResponse(200, JOB)
          : undefined,
      (url) =>
        url.endsWith('/api/v1/jobs/5/cancel')
          ? jsonResponse(200, {
              ...JOB,
              status: 'cancel_requested',
              cancel_requested: true,
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="cancel-job"]').trigger('click')
    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes('/cancel'),
      ),
    ).toBe(false)

    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const cancelCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/cancel'),
    )
    expect(cancelCall).toBeDefined()
    expect(wrapper.get('[data-testid="job-status"]').text()).toContain(
      'cancel requested',
    )
    expect(bodyOf(cancelCall!)).toEqual({})
  })

  it('renders partial failure alongside completed work', async () => {
    detailRoutes({
      status: 'failed',
      progress: { phase: 'sheets', completed: 3, failed: 2, total: 5 },
      error: { code: 'InfoSheetError', message: '2 sheets failed', retryable: false },
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    const banner = wrapper.get('[data-testid="partial-failure"]').text()
    expect(banner).toContain('2 unit(s) failed')
    expect(banner).toContain('3 completed')
    expect(wrapper.get('[data-testid="job-error-code"]').text()).toBe(
      'InfoSheetError',
    )
  })

  it('explains recovery_required failures readably', async () => {
    detailRoutes({
      status: 'failed',
      error: {
        code: 'recovery_required',
        message: 'worker lease expired',
        retryable: false,
      },
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="job-error-detail"]').text()).toContain(
      'not retried automatically',
    )
    expect(wrapper.get('[data-testid="job-error-detail"]').text()).toContain(
      'worker lease expired',
    )
  })

  it('links affected entities from the result', async () => {
    detailRoutes({
      job_type: 'match',
      status: 'succeeded',
      result: {
        unmatched_family_ids: [7],
        unmatched_sponsor_ids: [9],
        run_id: 3,
      },
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    const links = wrapper.findAllComponents(RouterLinkStub)
    const targets = links.map((link) => link.props('to'))
    expect(targets).toContain('/audit/families/7')
    expect(targets).toContain('/audit/sponsors/9')
    expect(wrapper.get('[data-testid="result-links"]').text()).toContain(
      'Unmatched family 7',
    )
  })

  it('renders log rows in the backend log pane', async () => {
    detailRoutes({
      events: [
        { id: 1, kind: 'started', ts: '2026-11-19T10:00:01Z', data: {} },
        {
          id: 2,
          kind: 'log',
          ts: '2026-11-19T10:00:02Z',
          data: {
            level: 'INFO',
            logger: 'ccat.match',
            message: 'Matching started',
            ts: '2026-11-19T10:00:02Z',
          },
        },
        {
          id: 3,
          kind: 'log',
          ts: '2026-11-19T10:00:03Z',
          data: {
            level: 'ERROR',
            logger: 'ccat.match',
            message: 'Something failed\non two lines',
            ts: '2026-11-19T10:00:03Z',
          },
        },
      ],
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    const lines = wrapper.findAll('[data-testid="job-log-line"]')
    expect(lines).toHaveLength(2)
    expect(lines[0]!.text()).toContain('INFO')
    expect(lines[0]!.text()).toContain('ccat.match')
    expect(lines[0]!.text()).toContain('Matching started')
    expect(lines[1]!.text()).toContain('ERROR')
    expect(lines[1]!.text()).toContain('Something failed')
    expect(lines[1]!.text()).toContain('on two lines')
    expect(lines[1]!.find('.log-level-error').exists()).toBe(true)
    expect(wrapper.find('[data-testid="job-log-empty"]').exists()).toBe(false)
    // Log rows live in the log pane only; the Events card keeps lifecycle rows.
    const eventItems = wrapper.findAll('[data-testid="job-events"] li')
    expect(eventItems).toHaveLength(1)
    expect(eventItems[0]!.text()).toContain('started')
  })

  it('does not render non-log events in the backend log pane', async () => {
    detailRoutes({
      events: [
        { id: 1, kind: 'started', ts: '2026-11-19T10:00:01Z', data: {} },
        {
          id: 2,
          kind: 'progress',
          ts: '2026-11-19T10:00:02Z',
          data: { phase: 'ai', completed: 1, total: 10 },
        },
        {
          id: 3,
          kind: 'log',
          ts: '2026-11-19T10:00:03Z',
          data: {
            level: 'INFO',
            logger: 'ccat',
            message: 'only log',
            ts: '2026-11-19T10:00:03Z',
          },
        },
      ],
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    const lines = wrapper.findAll('[data-testid="job-log-line"]')
    expect(lines).toHaveLength(1)
    expect(lines[0]!.text()).toContain('only log')
    expect(wrapper.find('[data-testid="job-events"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="job-events"]').text()).toContain(
      'started',
    )
    expect(wrapper.get('[data-testid="job-events"]').text()).toContain(
      'progress',
    )
  })

  it('shows an empty state when there are no log rows', async () => {
    detailRoutes({
      events: [{ id: 1, kind: 'started', ts: '2026-11-19T10:00:01Z', data: {} }],
    })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="job-log-empty"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="job-log-empty"]').text()).toBe(
      'No log output yet.',
    )
  })

  it('shows a distinct empty state when a finished job has no logs', async () => {
    detailRoutes({ status: 'succeeded', events: [] })
    const { wrapper } = await mountRouted(JobDetailView, {
      path: '/jobs/5',
      setup: withSeason,
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="job-log-empty"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="job-log-empty"]').text()).toBe(
      'Job finished with no log output.',
    )
  })
})
