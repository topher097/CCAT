import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import MatchView from '../src/views/MatchView.vue'
import { useJobPopup } from '../src/composables/useJobPopup'
import { useSessionStore } from '../src/stores/session'
import type {
  ManualAssignmentListItem,
  ManualMatchChildCard,
  ManualMatchFamilyCard,
  ManualMatchSponsor,
} from '../src/api/manual-match'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
}

/** A session that may enqueue a run (the API's coordinator/admin rule). */
function withCoordinator(): void {
  const session = useSessionStore()
  withSeason()
  session.me = {
    username: 'coord',
    roles: ['coordinator'],
    csrf_token: 'csrf',
  }
}

function child(
  overrides: Partial<ManualMatchChildCard> = {},
): ManualMatchChildCard {
  return {
    zite_record_id: '7A',
    angel_id: '7A',
    first_name: 'Riley',
    age_at_xmas: 8,
    gender: 'Male',
    special_needs: false,
    need_1: 'bike',
    need_2: 'helmet',
    desire_1: 'Lego set',
    desire_2: null,
    interests: 'soccer',
    other_notes: 'allergy to peanuts',
    favorite_colors: null,
    sizes: { shoe: '5 Youth', shirt: 'M Youth' },
    status: 'audited',
    assigned_sponsor_id: null,
    assignment_source: null,
    generated_info_sheet: false,
    row_version: 1,
    selectable: true,
    unselectable_reason: null,
    ...overrides,
  }
}

function family(
  overrides: Partial<ManualMatchFamilyCard> = {},
): ManualMatchFamilyCard {
  return {
    family_id: 7,
    row_version: 3,
    first_name: 'Jane',
    last_name: 'Doe',
    primary_email: 'jane@example.org',
    phone: '555-0100',
    city: 'Hancock',
    zip_code: '49930',
    state: 'MI',
    gift_pickup_location: 'Hancock',
    status: 'audited',
    family_size: 2,
    assigned_count: 0,
    assigned_sponsor_ids: [],
    assignment_state: 'unassigned',
    children: [
      child(),
      child({
        zite_record_id: '7B',
        angel_id: '7B',
        first_name: 'Sam',
        age_at_xmas: 11,
        gender: 'Female',
      }),
    ],
    hold: { held: false, note: null, created_by: null, created_at: null },
    warnings: [],
    ...overrides,
  }
}

function sponsor(
  overrides: Partial<ManualMatchSponsor> = {},
): ManualMatchSponsor {
  return {
    sponsor_id: 42,
    row_version: 5,
    display_name: 'Ada Lovelace',
    email: 'ada@example.org',
    phone: null,
    sponsor_type: 'Individual',
    num_children: 3,
    assigned_count: 1,
    remaining: 2,
    age_preference: 'Any',
    gender_preference: 'Any Gender',
    dropoff_location: 'Hancock',
    pref_age_min: 5,
    pref_age_max: 12,
    status: 'audited',
    first_name: 'Ada',
    last_name: 'Lovelace',
    organization_name: null,
    elevated_budget: false,
    physical_packet: false,
    packets_dispatched: false,
    packets_dispatched_at: null,
    ...overrides,
  }
}

function pool(
  items: ManualMatchFamilyCard[],
  automaticRun: Record<string, unknown> = {
    active: false,
    job_id: null,
    status: null,
    started_at: null,
  },
) {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 25,
    automatic_run: automaticRun,
  }
}

function poolHandler(
  build: (url: string) => ReturnType<typeof pool>,
): (url: string, init: RequestInit) => Response | undefined {
  return (url, init) =>
    url.includes('/manual-match/pool') && (init.method ?? 'GET') === 'GET'
      ? jsonResponse(200, build(url))
      : undefined
}

function sponsorsHandler(
  items: ManualMatchSponsor[],
): (url: string, init: RequestInit) => Response | undefined {
  return (url, init) =>
    url.includes('/manual-match/sponsors') && (init.method ?? 'GET') === 'GET'
      ? jsonResponse(200, {
          items,
          total: items.length,
          page: 1,
          page_size: 200,
        })
      : undefined
}

/** One assignment row as the angels popup endpoint returns it. */
function angelItem(
  overrides: Partial<ManualAssignmentListItem> = {},
): ManualAssignmentListItem {
  return {
    zite_record_id: '7A',
    angel_id: 'A12',
    family_id: 7,
    sponsor_id: 42,
    assignment_source: 'manual',
    first_name: 'Riley',
    age_at_xmas: 8,
    need_1: 'bike',
    need_2: 'helmet',
    desire_1: 'Lego set',
    desire_2: 'art kit',
    dropoff_location: 'Hancock',
    last_updated_by: null,
    last_updated_at: null,
    ...overrides,
  }
}

function assignmentsHandler(
  items: ManualAssignmentListItem[],
): (url: string, init: RequestInit) => Response | undefined {
  return (url, init) =>
    url.includes('/manual-match/assignments') && (init.method ?? 'GET') === 'GET'
      ? jsonResponse(200, {
          items,
          total: items.length,
          page: 1,
          page_size: 200,
        })
      : undefined
}

/** Deep link: the page opens already shopping for sponsor 42. */
const SHOPPING_PATH = '/match?sponsor_id=42'

async function mountPage(
  handlers: Array<
    (url: string, init: RequestInit) => Response | undefined
  >,
) {
  const fetchMock = mockRoutes(handlers)
  const { wrapper, router } = await mountRouted(MatchView, {
    path: SHOPPING_PATH,
    setup: withSeason,
  })
  await flushPromises()
  return { wrapper, router, fetchMock }
}

function postsTo(
  fetchMock: ReturnType<typeof mockRoutes>,
  suffix: string,
): unknown[][] {
  return fetchMock.mock.calls.filter(
    (call) =>
      String(call[0]).endsWith(suffix) &&
      String((call[1] as RequestInit).method) === 'POST',
  ) as unknown[][]
}

function poolCalls(fetchMock: ReturnType<typeof mockRoutes>): unknown[][] {
  return fetchMock.mock.calls.filter((call) =>
    String(call[0]).includes('/manual-match/pool'),
  ) as unknown[][]
}

function findChip(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], text: string) {
  return wrapper.findAll('button.chip').find((button) => button.text() === text)!
}

beforeEach(() => {
  setActivePinia(createPinia())
  useJobPopup().closeJob()
  expect(useJobPopup().jobId.value).toBe(null)
})

describe('match page', () => {
  it('renders each child needs, wants, interests and notes', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])

    expect(wrapper.find('[data-testid="family-card"]').exists()).toBe(true)
    const text = wrapper.text()
    expect(text).toContain('bike, helmet')
    expect(text).toContain('Lego set')
    expect(text).toContain('soccer')
    expect(text).toContain('allergy to peanuts')
    expect(text).toContain('shoe 5 Youth')
  })

  it('sends filters as query parameters and mirrors them into the URL', async () => {
    const { wrapper, router, fetchMock } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])

    await wrapper.get('input[name="q"]').setValue('doe')
    await wrapper.get('form.filters').trigger('submit')
    await flushPromises()
    await findChip(wrapper, 'split').trigger('click')
    await flushPromises()

    const last = poolCalls(fetchMock).at(-1)!
    const params = new URL(String(last[0]), 'http://localhost').searchParams
    expect(params.get('q')).toBe('doe')
    expect(params.get('assignment')).toBe('split')
    expect(params.get('hold')).toBe('not_held')
    expect(params.get('sponsor_id')).toBe('42')

    expect(router.currentRoute.value.query.q).toBe('doe')
    expect(router.currentRoute.value.query.assignment).toBe('split')
    expect(router.currentRoute.value.query.sponsor_id).toBe('42')
  })

  it('selects every selectable child from the card select-all control', async () => {
    const third = child({
      zite_record_id: '7C',
      angel_id: null,
      first_name: 'Alex',
      selectable: false,
      unselectable_reason: 'no angel id',
    })
    const { wrapper } = await mountPage([
      poolHandler(() =>
        pool([family({ children: [child(), child({ zite_record_id: '7B' }), third], family_size: 3 })]),
      ),
      sponsorsHandler([sponsor()]),
    ])

    await wrapper.get('[data-testid="select-all"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '2 selected',
    )
    const boxes = wrapper.findAll('[data-testid="family-card"] input[type="checkbox"]')
    expect(boxes).toHaveLength(3)
    expect((boxes[0]!.element as HTMLInputElement).checked).toBe(true)
    expect((boxes[1]!.element as HTMLInputElement).checked).toBe(true)
    expect((boxes[2]!.element as HTMLInputElement).checked).toBe(false)
    expect((boxes[2]!.element as HTMLInputElement).disabled).toBe(true)
  })

  it('posts the assignment body and refreshes the summaries', async () => {
    const fetchMock = mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url, init) =>
        url.endsWith('/manual-match/assignments') &&
        (init.method ?? 'GET') === 'POST'
          ? jsonResponse(200, {
              assigned: [
                { zite_record_id: '7A', angel_id: '7A', family_id: 7 },
                { zite_record_id: '7B', angel_id: '7B', family_id: 7 },
              ],
              reassigned: [],
              released: [],
              unchanged: [],
              warnings: [],
              families: [],
              sponsors: [],
              versions: {},
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="select-all"]').trigger('click')
    await wrapper.get('[data-testid="assign"]').trigger('click')
    await flushPromises()

    const posts = postsTo(fetchMock, '/manual-match/assignments')
    expect(posts).toHaveLength(1)
    expect(bodyOf(posts[0]!)).toEqual({
      sponsor_id: 42,
      family_ids: [7],
      child_ids: [],
      expected_versions: {
        'child:7A': 1,
        'child:7B': 1,
        'family:7': 3,
        'sponsor:42': 5,
      },
      confirm: false,
      note: null,
    })
    expect(poolCalls(fetchMock).length).toBeGreaterThan(1)
  })

  it('keeps the selection when the page changes', async () => {
    const pageOne = {
      items: [family()],
      total: 30,
      page: 1,
      page_size: 25,
      automatic_run: { active: false, job_id: null, status: null, started_at: null },
    }
    const pageTwo = {
      items: [family({ family_id: 8, first_name: 'Other' })],
      total: 30,
      page: 2,
      page_size: 25,
      automatic_run: { active: false, job_id: null, status: null, started_at: null },
    }
    const { wrapper } = await mountPage([
      (url, init) => {
        if (!url.includes('/manual-match/pool') || (init.method ?? 'GET') !== 'GET') {
          return undefined
        }
        const page = new URL(url, 'http://localhost').searchParams.get('page')
        return jsonResponse(200, page === '2' ? pageTwo : pageOne)
      },
      sponsorsHandler([sponsor()]),
    ])

    await wrapper.findAll('[data-testid="family-card"] input[type="checkbox"]')[0]!.setValue(true)
    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '1 selected',
    )

    const next = wrapper
      .findAll('.pagination button')
      .find((button) => button.text() === 'Next')!
    await next.trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '1 selected',
    )
  })

  it('shows the reload prompt instead of failing silently on a 412', async () => {
    mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url, init) =>
        url.endsWith('/manual-match/assignments') &&
        (init.method ?? 'GET') === 'POST'
          ? jsonResponse(412, {
              detail: { code: 'stale_version', message: 'child 7A is stale' },
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="select-all"]').trigger('click')
    await wrapper.get('[data-testid="assign"]').trigger('click')
    await flushPromises()

    const prompt = wrapper.get('[data-testid="stale-prompt"]')
    expect(prompt.text().toLowerCase()).toContain('reload')
    expect(wrapper.find('[data-testid="assign-error"]').exists()).toBe(false)
  })

  it('requires the release confirmation before anything is cleared', async () => {
    const split = family({
      assigned_count: 1,
      assigned_sponsor_ids: [42],
      assignment_state: 'partial',
      children: [
        child({ assigned_sponsor_id: 42, assignment_source: 'manual' }),
        child({ zite_record_id: '7B' }),
      ],
    })
    const fetchMock = mockRoutes([
      poolHandler(() => pool([split])),
      sponsorsHandler([sponsor()]),
      (url, init) =>
        url.endsWith('/manual-match/release') &&
        (init.method ?? 'GET') === 'POST'
          ? jsonResponse(200, {
              assigned: [],
              reassigned: [],
              released: [{ zite_record_id: '7A', family_id: 7, from_sponsor_id: 42 }],
              unchanged: [],
              warnings: [],
              families: [],
              sponsors: [],
              versions: {},
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.findAll('[data-testid="family-card"] input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.get('[data-testid="release"]').trigger('click')
    await flushPromises()

    expect(postsTo(fetchMock, '/manual-match/release')).toHaveLength(0)
    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '1 selected',
    )

    await wrapper.get('[data-testid="confirm"]').trigger('click')
    await flushPromises()

    const posts = postsTo(fetchMock, '/manual-match/release')
    expect(posts).toHaveLength(1)
    expect(bodyOf(posts[0]!)).toMatchObject({
      child_ids: ['7A'],
      confirm: true,
    })
  })

  it('renders the empty and error states with the house test ids', async () => {
    const empty = await mountPage([
      poolHandler(() => pool([])),
      sponsorsHandler([sponsor()]),
    ])
    expect(empty.wrapper.find('[data-testid="list-empty"]').exists()).toBe(true)

    const failed = await mountPage([
      (url) =>
        url.includes('/manual-match/pool')
          ? jsonResponse(500, {
              detail: { code: 'server_error', message: 'pool exploded' },
            })
          : undefined,
    ])
    expect(failed.wrapper.get('[data-testid="list-error"]').text()).toContain(
      'pool exploded',
    )
  })

  it('shows the Split badge for a split family', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() =>
        pool([
          family({
            assignment_state: 'split',
            assigned_count: 1,
            assigned_sponsor_ids: [42, 43],
          }),
        ]),
      ),
      sponsorsHandler([sponsor()]),
    ])

    expect(wrapper.get('[data-testid="assignment-state"]').text()).toContain(
      'Split',
    )
    expect(
      wrapper
        .get('[data-testid="family-card"]')
        .attributes('data-assignment-state'),
    ).toBe('split')
  })

  it('disables Assign and names the running job while a match is active', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() =>
        pool([family()], {
          active: true,
          job_id: 12,
          status: 'running',
          started_at: '2026-11-21T18:04:11Z',
        }),
      ),
      sponsorsHandler([sponsor()]),
    ])

    const notice = wrapper.get('[data-testid="automatic-running"]')
    expect(notice.text()).toContain('job #12')
    expect(notice.text()).toContain('paused')
    expect(
      wrapper.get('[data-testid="assign"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('marks a full sponsor unselectable and explains the cap', async () => {
    const full = sponsor({ assigned_count: 3, remaining: 0, num_children: 3 })
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([full]),
    ])

    const radio = wrapper.get('[data-testid="sponsor-row-radio"][value="42"]')
    expect(radio.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="sponsor-row-full"]').exists()).toBe(true)

    // The deep link still lands on the full sponsor: the cap is explained.
    expect(wrapper.get('[data-testid="cap-warning"]').text()).toContain('full')
    expect(
      wrapper.get('[data-testid="assign"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('shows a held card and requires the hold note before submitting', async () => {
    const held = family({
      hold: {
        held: true,
        note: 'family moved out of the area',
        created_by: 'cmendres',
        created_at: '2026-11-21T10:00:00Z',
      },
    })
    const fetchMock = mockRoutes([
      poolHandler(() => pool([held])),
      sponsorsHandler([sponsor()]),
      (url, init) =>
        url.endsWith('/manual-match/holds') &&
        (init.method ?? 'GET') === 'POST'
          ? jsonResponse(200, { family_id: 7, held: true, note: 'moved away' })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()

    const cardText = wrapper.text()
    expect(cardText).toContain('family moved out of the area')
    expect(cardText).toContain('cmendres')
    expect(wrapper.find('[data-testid="release-hold"]').exists()).toBe(true)

    await wrapper.findAll('[data-testid="family-card"] input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.get('[data-testid="hold-family"]').trigger('click')
    await flushPromises()

    const confirm = wrapper.get('[data-testid="hold-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    expect(postsTo(fetchMock, '/manual-match/holds')).toHaveLength(0)

    await wrapper.get('[data-testid="hold-note"]').setValue('moved away')
    await confirm.trigger('click')
    await flushPromises()

    const posts = postsTo(fetchMock, '/manual-match/holds')
    expect(posts).toHaveLength(1)
    expect(bodyOf(posts[0]!)).toEqual({ family_id: 7, note: 'moved away' })
  })

  it('does not fetch the pool until a sponsor is chosen', async () => {
    const fetchMock = mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: '/match',
      setup: withSeason,
    })
    await flushPromises()

    expect(poolCalls(fetchMock)).toHaveLength(0)
    expect(wrapper.find('[data-testid="pick-sponsor-first"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="family-card"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="list-empty"]').exists()).toBe(false)
    expect(wrapper.find('.pagination').exists()).toBe(false)
  })

  it('fetches the pool with sponsor_id once a sponsor is picked', async () => {
    const fetchMock = mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])
    const { wrapper, router } = await mountRouted(MatchView, {
      path: '/match',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper
      .get('[data-testid="sponsor-row-radio"][value="42"]')
      .setValue()
    await flushPromises()

    const last = poolCalls(fetchMock).at(-1)!
    const params = new URL(String(last[0]), 'http://localhost').searchParams
    expect(params.get('sponsor_id')).toBe('42')
    expect(router.currentRoute.value.query.sponsor_id).toBe('42')
    expect(wrapper.find('[data-testid="pick-sponsor-first"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="family-card"]').exists()).toBe(true)
  })

  it('sends all_audited by default and omits it once unchecked', async () => {
    const { wrapper, router, fetchMock } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])
    const paramsOf = (call: unknown[]) =>
      new URL(String(call[0]), 'http://localhost').searchParams
    expect(paramsOf(poolCalls(fetchMock).at(-1)!).get('all_audited')).toBe('true')

    await wrapper.get('form.filters input[type="checkbox"]').setValue(false)
    await flushPromises()

    expect(paramsOf(poolCalls(fetchMock).at(-1)!).has('all_audited')).toBe(false)
    expect(router.currentRoute.value.query.all_audited).toBe('0')
  })

  it('sends info_sheet when a non-default option is selected', async () => {
    const { wrapper, router, fetchMock } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])
    const select = wrapper
      .findAll('form.filters select')
      .find((entry) =>
        entry.findAll('option').some((option) => option.text() === 'missing'),
      )!
    await select.setValue('missing')
    await flushPromises()

    const last = poolCalls(fetchMock).at(-1)!
    const params = new URL(String(last[0]), 'http://localhost').searchParams
    expect(params.get('info_sheet')).toBe('missing')
    expect(router.currentRoute.value.query.info_sheet).toBe('missing')
  })

  it('opens the family details modal and closes it again', async () => {
    mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url) =>
        url.includes('/families/7')
          ? jsonResponse(200, {
              entity: 'family',
              key: '7',
              row: { family_id: 7, first_name: 'Jane', last_name: 'Doe' },
              baseline: {},
              submissions: [],
              status: 'audited',
              row_version: 3,
              last_updated_by: null,
              last_updated_at: null,
              audited_at: null,
              diagnostics: {},
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="entity-modal"]').exists()).toBe(false)

    await wrapper.get('[data-testid="open-family-details"]').trigger('click')
    await flushPromises()
    const modal = wrapper.get('[data-testid="entity-modal"]')
    expect(modal.attributes('aria-label')).toBe('Jane Doe')

    await wrapper.get('[data-testid="entity-modal-close"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="entity-modal"]').exists()).toBe(false)
  })

  it('opens the sponsor details modal from a sponsor row and closes it', async () => {
    mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url) =>
        url.includes('/sponsors/42') && !url.includes('/manual-match/')
          ? jsonResponse(200, {
              entity: 'sponsor',
              key: '42',
              row: { sponsor_id: 42, first_name: 'Ada', last_name: 'Lovelace' },
              baseline: {},
              submissions: [],
              status: 'audited',
              row_version: 5,
              last_updated_by: null,
              last_updated_at: null,
              audited_at: null,
              diagnostics: {},
            })
          : undefined,
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="entity-modal"]').exists()).toBe(false)

    await wrapper
      .get('[data-sponsor-id="42"] [data-testid="open-sponsor-details"]')
      .trigger('click')
    await flushPromises()
    const modal = wrapper.get('[data-testid="entity-modal"]')
    expect(modal.attributes('aria-label')).toContain('Ada Lovelace')

    await wrapper.get('[data-testid="entity-modal-close"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="entity-modal"]').exists()).toBe(false)
  })

  it('clears the selection when the sponsor changes', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor(), sponsor({ sponsor_id: 43, display_name: 'Bob' })]),
    ])
    await wrapper.get('[data-testid="select-all"]').trigger('click')
    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '2 selected',
    )

    await wrapper
      .get('[data-testid="sponsor-row-radio"][value="43"]')
      .setValue()
    await flushPromises()

    expect(wrapper.get('[data-testid="selection-count"]').text()).toContain(
      '0 selected',
    )
  })

  it('links to dispatch without posting and keeps the dispatched badge', async () => {
    const fetchMock = mockRoutes([
      poolHandler(() =>
        pool([family()], {
          active: true,
          job_id: 12,
          status: 'running',
          started_at: null,
        }),
      ),
      sponsorsHandler([sponsor({ packets_dispatched: true })]),
    ])
    const { wrapper } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withSeason,
    })
    await flushPromises()

    const link = wrapper.getComponent(RouterLinkStub)
    expect(link.props('to')).toEqual({
      path: '/dispatch',
      query: { sponsor: 42 },
    })
    expect(link.attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="sponsor-row-dispatched"]').text()).toBe(
      'Dispatched',
    )
    await link.trigger('click')
    expect(postsTo(fetchMock, '/manual-match/dispatch')).toHaveLength(0)
  })

  it('locks Assign and Release when the selected sponsor is dispatched', async () => {
    const assigned = family({
      assigned_count: 1,
      assigned_sponsor_ids: [42],
      assignment_state: 'partial',
      children: [
        child({ assigned_sponsor_id: 42, assignment_source: 'manual' }),
        child({ zite_record_id: '7B' }),
      ],
    })
    const { wrapper } = await mountPage([
      poolHandler(() => pool([assigned])),
      sponsorsHandler([sponsor({ packets_dispatched: true })]),
    ])

    expect(wrapper.find('[data-testid="pick-sponsor-first"]').exists()).toBe(false)
    await wrapper
      .findAll('[data-testid="family-card"] input[type="checkbox"]')[0]!
      .setValue(true)

    expect(wrapper.get('[data-testid="dispatched-warning"]').text()).toContain(
      'dispatched',
    )
    expect(wrapper.get('[data-testid="assign"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="release"]').attributes('disabled')).toBeDefined()
  })

})

describe('assigned angels popup', () => {
  it('lists every angel detail and fetches with the sponsor filter', async () => {
    const { wrapper, fetchMock } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      assignmentsHandler([
        angelItem(),
        angelItem({
          zite_record_id: '7B',
          angel_id: 'B7',
          first_name: 'Sam',
          age_at_xmas: 11,
          need_1: 'coat',
          need_2: null,
          desire_1: 'puzzle',
          desire_2: null,
          dropoff_location: 'Calumet',
        }),
      ]),
    ])

    await wrapper.get('[data-testid="assigned-angels-trigger"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="sponsor-angels-heading"]').text()).toBe(
      'Angels assigned to Ada Lovelace',
    )
    const rows = wrapper.findAll('[data-testid="sponsor-angel-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.get('[data-testid="sponsor-angel-id"]').text()).toBe('A12')
    expect(rows[0]!.get('[data-testid="sponsor-angel-name"]').text()).toBe('Riley')
    expect(rows[0]!.get('[data-testid="sponsor-angel-age"]').text()).toBe('8')
    expect(rows[0]!.get('[data-testid="sponsor-angel-needs"]').text()).toBe('bike | helmet')
    expect(rows[0]!.get('[data-testid="sponsor-angel-wishes"]').text()).toBe('Lego set | art kit')
    expect(rows[0]!.get('[data-testid="sponsor-angel-dropoff"]').text()).toBe('Hancock')
    expect(rows[1]!.get('[data-testid="sponsor-angel-id"]').text()).toBe('B7')
    expect(rows[1]!.get('[data-testid="sponsor-angel-name"]').text()).toBe('Sam')
    expect(rows[1]!.get('[data-testid="sponsor-angel-age"]').text()).toBe('11')
    expect(rows[1]!.get('[data-testid="sponsor-angel-needs"]').text()).toBe('coat')
    expect(rows[1]!.get('[data-testid="sponsor-angel-wishes"]').text()).toBe('puzzle')
    expect(rows[1]!.get('[data-testid="sponsor-angel-dropoff"]').text()).toBe('Calumet')

    const calls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes('/manual-match/assignments'),
    )
    expect(calls).toHaveLength(1)
    const params = new URL(String(calls[0]![0]), 'http://localhost').searchParams
    expect(params.get('sponsor_id')).toBe('42')
    expect(params.get('page_size')).toBe('200')
  })

  it('opens the child audit popup on top of the angels list', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      assignmentsHandler([angelItem()]),
      (url) =>
        url.includes('/children/7A')
          ? jsonResponse(200, {
              entity: 'child',
              key: '7A',
              row: {
                zite_record_id: '7A',
                angel_id: 'A12',
                first_name: 'Riley',
                family_id: 7,
              },
              baseline: {},
              submissions: [],
              status: 'audited',
              row_version: 1,
              last_updated_by: null,
              last_updated_at: null,
              audited_at: null,
              diagnostics: {},
            })
          : undefined,
    ])

    await wrapper.get('[data-testid="assigned-angels-trigger"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="entity-modal"]').exists()).toBe(false)

    await wrapper.get('[data-testid="sponsor-angel-row"]').trigger('click')
    await flushPromises()

    const modal = wrapper.get('[data-testid="entity-modal"]')
    expect(modal.attributes('aria-label')).toBe('Riley')
    // The angels list stays open behind the audit popup.
    expect(wrapper.find('[data-testid="sponsor-angels-dialog"]').exists()).toBe(true)
  })

  it('shows the empty state for a sponsor with no assigned angels', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor({ assigned_count: 0 })]),
      assignmentsHandler([]),
    ])

    await wrapper.get('[data-testid="assigned-angels-trigger"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="sponsor-angels-empty"]').text()).toBe(
      'No angels assigned.',
    )
    expect(wrapper.find('[data-testid="sponsor-angel-row"]').exists()).toBe(false)
  })

  it('keeps the popup open with a retry when the request fails', async () => {
    let fail = true
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url, init) => {
        if (
          !url.includes('/manual-match/assignments') ||
          (init.method ?? 'GET') !== 'GET'
        ) {
          return undefined
        }
        return fail
          ? jsonResponse(500, {
              detail: { code: 'server_error', message: 'angels exploded' },
            })
          : jsonResponse(200, {
              items: [angelItem()],
              total: 1,
              page: 1,
              page_size: 200,
            })
      },
    ])

    await wrapper.get('[data-testid="assigned-angels-trigger"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="sponsor-angels-error"]').text()).toContain(
      'angels exploded',
    )

    fail = false
    await wrapper.get('[data-testid="sponsor-angels-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="sponsor-angels-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="sponsor-angel-row"]')).toHaveLength(1)
  })

  it('follows pagination until every angel is collected', async () => {
    const pageOne = Array.from({ length: 200 }, (_, i) =>
      angelItem({ zite_record_id: `C${i}`, angel_id: `A${i}` }),
    )
    const { wrapper, fetchMock } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url, init) => {
        if (
          !url.includes('/manual-match/assignments') ||
          (init.method ?? 'GET') !== 'GET'
        ) {
          return undefined
        }
        const page = new URL(url, 'http://localhost').searchParams.get('page')
        return jsonResponse(200, {
          items:
            page === '2'
              ? [angelItem({ zite_record_id: 'C200', angel_id: 'A200' })]
              : pageOne,
          total: 201,
          page: Number(page ?? 1),
          page_size: 200,
        })
      },
    ])

    await wrapper.get('[data-testid="assigned-angels-trigger"]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[data-testid="sponsor-angel-row"]')).toHaveLength(201)
    const calls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes('/manual-match/assignments'),
    )
    expect(calls).toHaveLength(2)
  })
})

describe('automatic matcher', () => {
  const MATCH_SETTINGS = {
    command: 'match',
    run: { test: false, skip_match: false },
    config_path: 'config/ccat_2026.json',
    editable: true,
    saved: false,
  }

  function settingsHandler(
    url: string,
    init: RequestInit,
  ): Response | undefined {
    if (url.endsWith('/runs/match/settings') && (init.method ?? 'GET') === 'GET') {
      return jsonResponse(200, MATCH_SETTINGS)
    }
    if (url.endsWith('/seasons/2026/jobs') && init.method === 'POST') {
      return jsonResponse(202, {
        id: 12,
        job_type: 'match',
        status: 'queued',
      })
    }
    return undefined
  }

  it('runs the matcher as one job with the stored flags', async () => {
    const fetchMock = mockRoutes([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      settingsHandler,
    ])
    const { wrapper, router } = await mountRouted(MatchView, {
      path: SHOPPING_PATH,
      setup: withCoordinator,
    })
    await flushPromises()

    await wrapper.get('[data-testid="run-automatic-match"]').trigger('click')
    await flushPromises()

    const posts = postsTo(fetchMock, '/seasons/2026/jobs')
    expect(posts).toHaveLength(1)
    expect(bodyOf(posts[0]!)).toEqual({
      job_type: 'match',
      options: { test: false, skip_match: false },
      idempotency_key: expect.any(String),
    })
    expect(useJobPopup().jobId.value).toBe(12)
    expect(router.currentRoute.value.path).toBe('/match')
  })

  it('hides the button from a role that may not run it', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
    ])

    expect(wrapper.find('[data-testid="run-automatic-match"]').exists()).toBe(false)
    expect(wrapper.get('h1').text()).toBe('Match')
    expect(wrapper.text()).toContain('coordinator or admin')
  })

  it('lists the rows of the last match report', async () => {
    const { wrapper } = await mountPage([
      poolHandler(() => pool([family()])),
      sponsorsHandler([sponsor()]),
      (url) =>
        url.includes('/diagnostics/match-report')
          ? jsonResponse(200, [
              {
                kind: 'unmatched_family',
                ref: '1',
                reason: 'no sponsor capacity',
              },
            ])
          : undefined,
    ])
    await flushPromises()

    const rows = wrapper.get('[data-testid="match-report"]').findAll('tbody tr')
    expect(rows).toHaveLength(1)
    expect(rows[0]!.text()).toContain('unmatched_family')
    expect(rows[0]!.text()).toContain('no sponsor capacity')
  })
})
