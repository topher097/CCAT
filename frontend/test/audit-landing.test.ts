import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

import AuditLandingView from '../src/views/AuditLandingView.vue'
import { useSessionStore } from '../src/stores/session'
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
      by_status: { new: 2, audited: 1 },
      ignored: 0,
      orphans: 0,
      raw_missing: 1,
      pending_ai: 0,
      generated: 0,
      assigned: 1,
    },
    sponsor: {
      total: 2,
      by_status: { new: 1, audited: 1 },
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
  artifacts: {},
  last_jobs: {},
  data_revision: '2026-11-01T10:00:00Z',
}

function mountLanding() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
  return mount(AuditLandingView, {
    global: {
      plugins: [pinia],
      stubs: { RouterLink: RouterLinkStub },
    },
  })
}

function mockOverview() {
  mockRoutes([
    (url) =>
      url.includes('/overview') ? jsonResponse(200, OVERVIEW) : undefined,
    (url) =>
      url.includes('/diagnostics/')
        ? jsonResponse(200, [])
        : undefined,
  ])
}

beforeEach(() => setActivePinia(createPinia()))

describe('audit landing', () => {
  it('offers one card per audit section with that section progress', async () => {
    mockOverview()
    const wrapper = mountLanding()
    await flushPromises()

    const sections = wrapper.get('[data-testid="audit-sections"]')
    const text = sections.text()
    expect(text).toContain('Children')
    expect(text).toContain('Families')
    expect(text).toContain('Sponsors')
    expect(text).toContain('Duplications')
    // The children's progress: 12 rows, 3 audited.
    expect(text).toContain('12')
    expect(text).toContain('audited')
    // The duplicate groups carry their own status vocabulary.
    expect(text).toContain('pending')
    expect(text).toContain('resolved')
  })

  it('links each card to its audit section', async () => {
    mockOverview()
    const wrapper = mountLanding()
    await flushPromises()

    const targets = wrapper
      .findAllComponents({ name: 'RouterLinkStub' })
      .map((link) => link.props('to'))
    expect(targets).toContain('/audit/children')
    expect(targets).toContain('/audit/families')
    expect(targets).toContain('/audit/sponsors')
    expect(targets).toContain('/audit/duplications')
  })
})
