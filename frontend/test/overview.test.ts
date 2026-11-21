import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

import HomeView from '../src/views/HomeView.vue'
import { useSessionStore } from '../src/stores/session'
import { useOverviewStore } from '../src/stores/overview'
import { jsonResponse, mockRoutes } from './helpers'

const OVERVIEW = {
  season: '2026',
  entities: {
    child: {
      total: 12,
      by_status: {
        new: 5,
        in_progress: 2,
        needs_clarification: 1,
        ignored: 1,
        audited: 3,
      },
      ignored: 1,
      orphans: 2,
      raw_missing: 0,
      pending_ai: 4,
      generated: 1,
      assigned: 0,
    },
    family: {
      total: 3,
      by_status: { new: 2, in_progress: 0, needs_clarification: 0, ignored: 0, audited: 1 },
      ignored: 0,
      orphans: 0,
      raw_missing: 1,
      pending_ai: 0,
      generated: 0,
      assigned: 1,
    },
    sponsor: {
      total: 2,
      by_status: { new: 1, in_progress: 0, needs_clarification: 0, ignored: 0, audited: 1 },
      ignored: 0,
      orphans: 0,
      raw_missing: 0,
      pending_ai: 0,
      generated: 0,
      assigned: 2,
    },
    duplication: {
      total: 4,
      by_status: { pending: 2, resolved: 1, audited: 1 },
    },
  },
  diagnostics: {
    duplicate_groups: 3,
    dob_conflicts: 1,
    email_checks: 9,
    address_checks: 4,
  },
  artifacts: { packet: 2 },
  last_jobs: {
    generate: {
      id: 42,
      status: 'succeeded',
      created_at: '2026-11-19T10:00:00Z',
    },
  },
  data_revision: '2026-11-01T10:00:00Z',
}

function mountHome() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
  return mount(HomeView, {
    global: {
      plugins: [pinia],
      stubs: { RouterLink: RouterLinkStub },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('home overview', () => {
  it('shows a loading state while the overview request is pending', async () => {
    let resolve: ((value: Response) => void) | undefined
    mockRoutes([
      () =>
        new Promise<Response>((done) => {
          resolve = done
        }),
    ])
    const wrapper = mountHome()
    await flushPromises()
    expect(wrapper.find('[data-testid="overview-loading"]').exists()).toBe(true)
    resolve?.(jsonResponse(200, OVERVIEW))
    await flushPromises()
    wrapper.unmount()
  })

  it('renders server counts, diagnostics, artifacts and jobs', async () => {
    mockRoutes([() => jsonResponse(200, OVERVIEW)])
    const wrapper = mountHome()
    await flushPromises()

    const cards = wrapper.get('[data-testid="overview-cards"]')
    expect(cards.text()).toContain('Children')
    expect(cards.text()).toContain('pending AI 4')
    expect(cards.text()).toContain('raw missing 1')
    expect(cards.text()).toContain('Duplications')
    expect(cards.text()).toContain('pending 2')
    expect(cards.text()).toContain('resolved 1')
    expect(wrapper.text()).toContain('duplicate groups:')
    expect(wrapper.text()).toContain('packet:')
    expect(wrapper.text()).toContain('generate')
    expect(wrapper.get('[data-testid="overview-jobs"]').text()).toContain(
      'succeeded',
    )

    // The duplications card sits right after the sponsors card.
    const links = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .map((link) => link.props('to'))
    expect(links.indexOf('/audit/duplications')).toBe(
      links.indexOf('/audit/sponsors') + 1,
    )
  })

  it('marks an old data revision as stale and a null revision too', async () => {
    mockRoutes([() => jsonResponse(200, OVERVIEW)])
    const wrapper = mountHome()
    await flushPromises()
    expect(wrapper.find('[data-testid="overview-stale"]').exists()).toBe(true)

    mockRoutes([
      () => jsonResponse(200, { ...OVERVIEW, data_revision: null }),
    ])
    const second = mountHome()
    await flushPromises()
    expect(second.find('[data-testid="overview-stale"]').exists()).toBe(true)
    second.unmount()
  })

  it('renders the error state with a retry', async () => {
    mockRoutes([
      () =>
        jsonResponse(500, {
          detail: { code: 'internal_error', message: 'internal server error' },
        }),
    ])
    const wrapper = mountHome()
    await flushPromises()
    expect(wrapper.get('[data-testid="overview-error"]').text()).toContain(
      'internal server error',
    )
    expect(wrapper.get('[data-testid="overview-error"]').text()).toContain(
      'Retry',
    )
  })

  it('shows an empty-season state when all entity counts are zero', async () => {
    const empty = {
      ...OVERVIEW,
      entities: {
        child: { total: 0, by_status: {} },
        family: { total: 0, by_status: {} },
        sponsor: { total: 0, by_status: {} },
      },
    }
    mockRoutes([() => jsonResponse(200, empty)])
    const wrapper = mountHome()
    await flushPromises()
    expect(wrapper.find('[data-testid="overview-empty"]').exists()).toBe(true)
  })

  it('polls every 30s but skips work while the document is hidden', async () => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    const fetchMock = mockRoutes([() => jsonResponse(200, OVERVIEW)])
    const store = useOverviewStore()
    await store.refresh('2026')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    store.startPolling('2026')

    Object.defineProperty(document, 'hidden', {
      configurable: true,
      value: true,
    })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    Object.defineProperty(document, 'hidden', {
      configurable: true,
      value: false,
    })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    store.stopPolling()
    vi.useRealTimers()
    delete (document as { hidden?: boolean }).hidden
  })
})
