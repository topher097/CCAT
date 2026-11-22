import { beforeEach, describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import ManualMatchView from '../src/views/ManualMatchView.vue'
import SponsorPicker from '../src/components/SponsorPicker.vue'
import { useSessionStore } from '../src/stores/session'
import type {
  ManualMatchChildCard,
  ManualMatchFamilyCard,
  ManualMatchSponsor,
} from '../src/api/manual-match'
import { bodyOf, jsonResponse, mockRoutes, mountRouted } from './helpers'

function withSeason(): void {
  useSessionStore().season = { id: 1, name: '2026', xmas_date: '2026-12-25' }
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

async function mountPage(
  handlers: Array<
    (url: string, init: RequestInit) => Response | undefined
  >,
) {
  const fetchMock = mockRoutes(handlers)
  const { wrapper, router } = await mountRouted(ManualMatchView, {
    path: '/match/manual',
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

beforeEach(() => setActivePinia(createPinia()))

describe('manual matching page', () => {
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

    expect(router.currentRoute.value.query.q).toBe('doe')
    expect(router.currentRoute.value.query.assignment).toBe('split')
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
    const boxes = wrapper.findAll('input[type="checkbox"]')
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
    const { wrapper } = await mountRouted(ManualMatchView, {
      path: '/match/manual',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="select-all"]').trigger('click')
    await wrapper.get('[data-testid="sponsor-picker"]').setValue('42')
    await flushPromises()
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

    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
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
    const { wrapper } = await mountRouted(ManualMatchView, {
      path: '/match/manual',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.get('[data-testid="select-all"]').trigger('click')
    await wrapper.get('[data-testid="sponsor-picker"]').setValue('42')
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
    const { wrapper } = await mountRouted(ManualMatchView, {
      path: '/match/manual',
      setup: withSeason,
    })
    await flushPromises()

    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
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

    const option = wrapper
      .findAll('[data-testid="sponsor-picker"] option')
      .find((entry) => entry.text().includes('Ada Lovelace — full'))!
    expect(option.attributes('disabled')).toBeDefined()

    wrapper.getComponent(SponsorPicker).vm.$emit('update:modelValue', 42)
    await flushPromises()

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
    const { wrapper } = await mountRouted(ManualMatchView, {
      path: '/match/manual',
      setup: withSeason,
    })
    await flushPromises()

    const cardText = wrapper.text()
    expect(cardText).toContain('family moved out of the area')
    expect(cardText).toContain('cmendres')
    expect(wrapper.find('[data-testid="release-hold"]').exists()).toBe(true)

    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
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
})
