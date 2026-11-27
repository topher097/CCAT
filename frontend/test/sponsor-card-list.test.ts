import { describe, expect, it } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'

import SponsorCardList, { type SponsorCard } from '../src/components/SponsorCardList.vue'

function sponsor(overrides: Partial<SponsorCard> = {}): SponsorCard {
  return {
    sponsor_id: 1,
    row_version: 1,
    display_name: 'Ada Lovelace',
    first_name: 'Ada',
    last_name: 'Lovelace',
    organization_name: 'Analytical Engines',
    email: 'ada@example.org',
    sponsor_type: 'Business',
    num_children: 3,
    assigned_count: 1,
    remaining: 2,
    age_preference: 'Teens',
    pref_age_min: 12,
    pref_age_max: 17,
    gender_preference: 'Girls',
    dropoff_location: 'North',
    status: 'in_progress',
    ...overrides,
  }
}

function mountList(sponsors: SponsorCard[], modelValue: number | null = null) {
  return mount(SponsorCardList, {
    props: { sponsors, modelValue },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

type Wrapper = ReturnType<typeof mountList>

const ids = (wrapper: Wrapper) =>
  wrapper.findAll('[data-testid="sponsor-row"]').map((row) => Number(row.attributes('data-sponsor-id')))

const count = (wrapper: Wrapper) => wrapper.get('[data-testid="sponsor-count"]').text()

/** Five sponsors, deliberately out of order, covering every filter dimension. */
function fixture(): SponsorCard[] {
  return [
    sponsor({
      sponsor_id: 30,
      first_name: 'Cy',
      last_name: 'Young',
      display_name: 'Cy Young',
      organization_name: null,
      email: 'cy@church.org',
      sponsor_type: 'Church',
      num_children: 4,
      assigned_count: 0,
      remaining: 4,
      status: 'new',
    }),
    sponsor({
      sponsor_id: 10,
      first_name: 'Bea',
      last_name: 'Arthur',
      display_name: 'Bea Arthur',
      organization_name: 'Golden Co',
      sponsor_type: 'Business',
      num_children: 3,
      remaining: 0,
      assigned_count: 3,
      status: 'audited',
    }),
    sponsor({
      sponsor_id: 20,
      first_name: 'Al',
      last_name: 'Zed',
      display_name: 'Al Zed',
      organization_name: null,
      sponsor_type: 'Church',
      num_children: 2,
      remaining: 1,
      assigned_count: 1,
      status: 'needs_clarification',
    }),
    sponsor({
      sponsor_id: 50,
      first_name: 'Di',
      last_name: 'Open',
      display_name: 'Di Open',
      organization_name: null,
      sponsor_type: null,
      num_children: null,
      remaining: null,
      status: null,
    }),
    sponsor({
      sponsor_id: 40,
      first_name: 'Ed',
      last_name: 'Done',
      display_name: 'Ed Done',
      organization_name: null,
      sponsor_type: 'Business',
      num_children: 2,
      remaining: 0,
      assigned_count: 2,
      packets_dispatched: true,
      status: 'audited',
    }),
  ]
}

describe('SponsorCardList rows', () => {
  it('renders one radio per row and emits the chosen id', async () => {
    const wrapper = mountList([sponsor(), sponsor({ sponsor_id: 2, display_name: 'Bo' })])
    const radios = wrapper.findAll('[data-testid="sponsor-row-radio"]')
    expect(radios).toHaveLength(2)
    expect(radios[0]!.attributes('name')).toBe('sponsor-shop')
    // Default order is sponsor # ascending, so #1 is first.
    expect(radios[0]!.attributes('aria-label')).toBe('Ada Lovelace, 2 of 3 remaining')

    await radios[1]!.setValue(true)
    expect(wrapper.emitted('update:modelValue')).toEqual([[2]])
  })

  it('shows id, name, organization, affiliation, status, counts and preferences', () => {
    const wrapper = mountList([sponsor()], 1)
    const row = wrapper.get('[data-testid="sponsor-row"]')
    expect(row.get('[data-testid="sponsor-row-id"]').text()).toBe('#1')
    expect(row.text()).toContain('Ada Lovelace')
    expect(row.text()).toContain('Analytical Engines')
    expect(row.text()).toContain('Business')
    expect(row.get('[data-testid="sponsor-row-status"]').text()).toBe('audit: in progress')
    expect(row.get('[data-testid="sponsor-row-status"]').classes()).toContain('status-in-progress')
    expect(row.text()).toContain('Teens (12–17)')
    expect(row.text()).toContain('Girls')
    expect(row.text()).toContain('North')
    expect(row.classes()).toContain('selected')
  })

  it('falls back from the person name to display name to the id', () => {
    const wrapper = mountList([
      sponsor({ sponsor_id: 5, first_name: null, last_name: null, display_name: 'Acme' }),
      sponsor({ sponsor_id: 6, first_name: null, last_name: null, display_name: '' }),
    ])
    expect(wrapper.findAll('.name').map((n) => n.text())).toEqual(['Acme', 'Sponsor 6'])
  })

  it('shows unlimited for a sponsor without a quota and a neutral unset status', () => {
    const wrapper = mountList([sponsor({ num_children: null, remaining: null, status: null })])
    const row = wrapper.get('[data-testid="sponsor-row"]')
    expect(row.text()).toContain('unlimited')
    expect(row.get('[data-testid="sponsor-row-status"]').text()).toBe('audit: unset')
    expect(row.get('[data-testid="sponsor-row-status"]').classes()).toContain('audit-unset')
    expect(row.get('[data-testid="sponsor-row-radio"]').attributes('disabled')).toBeUndefined()
  })

  it('disables a full sponsor, marks it, and links to the dispatch page', () => {
    const wrapper = mountList([sponsor({ remaining: 0, assigned_count: 3 })])
    expect(wrapper.find('[data-testid="sponsor-row-full"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="sponsor-row-radio"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="sponsor-row"]').classes()).toContain('unavailable')

    const link = wrapper.findComponent(RouterLinkStub)
    expect(link.props('to')).toEqual({ path: '/dispatch', query: { sponsor: 1 } })
    expect(wrapper.emitted('dispatch')).toBeUndefined()
  })

  it('colors the matched state: full green, partial yellow, empty red, uncapped none', () => {
    const full = mountList([sponsor({ remaining: 0, assigned_count: 3 })])
    expect(full.get('[data-testid="sponsor-row-full"]').classes()).toContain('check-ok')
    expect(full.get('[data-testid="sponsor-row-full"]').text()).toBe('full')

    const partial = mountList([sponsor({ remaining: 2, assigned_count: 1 })])
    expect(partial.get('[data-testid="sponsor-row-partial"]').classes()).toContain('check-warn')
    expect(partial.get('[data-testid="sponsor-row-partial"]').text()).toBe('1 of 3')

    const empty = mountList([sponsor({ remaining: 3, assigned_count: 0 })])
    expect(empty.get('[data-testid="sponsor-row-empty"]').classes()).toContain('check-bad')
    expect(empty.get('[data-testid="sponsor-row-empty"]').text()).toBe('3 open')

    const uncapped = mountList([sponsor({ num_children: null, remaining: null })])
    expect(uncapped.find('[data-testid="sponsor-row-full"]').exists()).toBe(false)
    expect(uncapped.find('[data-testid="sponsor-row-partial"]').exists()).toBe(false)
    expect(uncapped.find('[data-testid="sponsor-row-empty"]').exists()).toBe(false)
  })

  it('disables selection for an already dispatched sponsor and still links to dispatch', () => {
    const wrapper = mountList([sponsor({ remaining: 0, packets_dispatched: true })])
    expect(wrapper.find('[data-testid="sponsor-row-dispatched"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="sponsor-row-radio"]').attributes('disabled')).toBeDefined()
    const link = wrapper.findComponent(RouterLinkStub)
    expect(link.props('to')).toEqual({ path: '/dispatch', query: { sponsor: 1 } })
    expect(wrapper.emitted('dispatch')).toBeUndefined()
  })

  it('links every row to its own sponsor on the dispatch page', () => {
    const wrapper = mountList([sponsor({ sponsor_id: 1 }), sponsor({ sponsor_id: 7 })])
    const links = wrapper.findAllComponents(RouterLinkStub)
    expect(links.map((link) => link.props('to'))).toEqual([
      { path: '/dispatch', query: { sponsor: 1 } },
      { path: '/dispatch', query: { sponsor: 7 } },
    ])
  })

  it('shows budget and packet indicators only when true', () => {
    const on = mountList([sponsor({ elevated_budget: true, physical_packet: true })])
    expect(on.text()).toContain('elevated budget')
    expect(on.text()).toContain('physical packet')

    const off = mountList([sponsor({ elevated_budget: false, physical_packet: null })])
    expect(off.text()).not.toContain('elevated budget')
    expect(off.text()).not.toContain('physical packet')
  })

  it('shows email and phone, and nothing when they are absent', () => {
    const wrapper = mountList([
      sponsor({ sponsor_id: 1, email: 'ada@example.org', phone: '555-0100' }),
      sponsor({ sponsor_id: 2, email: null, phone: null }),
      sponsor({ sponsor_id: 3, email: 'only@example.org', phone: undefined }),
    ])
    const [first, second, third] = wrapper.findAll('[data-testid="sponsor-row"]')
    expect(first!.get('[data-testid="sponsor-row-email"]').text()).toBe('ada@example.org')
    // Full address stays reachable when the cell truncates it.
    expect(first!.get('[data-testid="sponsor-row-email"]').attributes('title')).toBe(
      'ada@example.org',
    )
    expect(first!.get('[data-testid="sponsor-row-phone"]').text()).toBe('555-0100')
    expect(second!.find('[data-testid="sponsor-row-email"]').exists()).toBe(false)
    expect(second!.find('[data-testid="sponsor-row-phone"]').exists()).toBe(false)
    expect(third!.find('[data-testid="sponsor-row-email"]').exists()).toBe(true)
    expect(third!.find('[data-testid="sponsor-row-phone"]').exists()).toBe(false)
  })

  it('opens details for any sponsor, including full and dispatched rows', async () => {
    const wrapper = mountList([
      sponsor({ sponsor_id: 1 }),
      sponsor({ sponsor_id: 2, remaining: 0, assigned_count: 3 }),
      sponsor({ sponsor_id: 3, remaining: 0, packets_dispatched: true }),
      sponsor({ sponsor_id: 4, first_name: null, last_name: null, display_name: '' }),
    ])
    const buttons = wrapper.findAll('[data-testid="open-sponsor-details"]')
    expect(buttons).toHaveLength(4)
    expect(buttons[0]!.attributes('aria-label')).toBe('View sponsor Ada Lovelace details')
    expect(buttons[3]!.attributes('aria-label')).toBe('View sponsor Sponsor 4 details')
    for (const button of buttons) {
      expect(button.attributes('disabled')).toBeUndefined()
      await button.trigger('click')
    }
    expect(wrapper.emitted('openDetails')).toEqual([[1], [2], [3], [4]])
    // Viewing never selects.
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('opens the assigned angels list from the Assigned count, even at zero', async () => {
    const wrapper = mountList([
      sponsor(),
      sponsor({ sponsor_id: 2, assigned_count: 0 }),
    ])
    const triggers = wrapper.findAll('[data-testid="assigned-angels-trigger"]')
    expect(triggers).toHaveLength(2)
    expect(triggers[0]!.text()).toBe('1')
    expect(triggers[0]!.attributes('aria-label')).toBe(
      'View assigned angels for Ada Lovelace, 1',
    )
    expect(triggers[1]!.text()).toBe('0')
    for (const trigger of triggers) {
      expect(trigger.attributes('disabled')).toBeUndefined()
      await trigger.trigger('click')
    }
    expect(wrapper.emitted('openAssignments')).toEqual([[1], [2]])
    // Viewing never selects.
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    // The label and count still render as plain text in the row.
    const rowText = wrapper.get('[data-testid="sponsor-row"]').text()
    expect(rowText).toContain('Wanted 3')
    // The Assigned gap matches the neighbouring counts.
    expect(rowText).toContain('Assigned 1')
    expect(rowText).toContain('Left 2')
  })

  it('keeps every row visible, with its dispatch link, when the host disables the list', () => {
    const wrapper = mount(SponsorCardList, {
      props: { sponsors: fixture(), modelValue: null, disabled: true },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    expect(ids(wrapper)).toHaveLength(5)
    for (const radio of wrapper.findAll('[data-testid="sponsor-row-radio"]')) {
      expect(radio.attributes('disabled')).toBeDefined()
    }
  })
})

describe('SponsorCardList ordering', () => {
  it('defaults to sponsor number ascending whatever the input order', () => {
    const wrapper = mountList(fixture())
    expect(ids(wrapper)).toEqual([10, 20, 30, 40, 50])
    expect((wrapper.get('[data-testid="sponsor-sort"]').element as HTMLSelectElement).value).toBe(
      'id-asc',
    )
  })

  it('sorts by sponsor number descending', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('id-desc')
    expect(ids(wrapper)).toEqual([50, 40, 30, 20, 10])
  })

  it('sorts by name A–Z', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('name')
    // Al Zed, Bea Arthur, Cy Young, Di Open, Ed Done
    expect(ids(wrapper)).toEqual([20, 10, 30, 50, 40])
  })

  it('sorts most remaining first, with unlimited counting as most room', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('remaining-most')
    // 50 unlimited, 30 has 4, 20 has 1, then the two full ones by number
    expect(ids(wrapper)).toEqual([50, 30, 20, 10, 40])
  })

  it('sorts fewest remaining first, with unlimited last', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('remaining-least')
    expect(ids(wrapper)).toEqual([10, 40, 20, 30, 50])
  })

  it('does not reorder the host list', () => {
    const list = fixture()
    const before = list.map((s) => s.sponsor_id)
    mountList(list)
    expect(list.map((s) => s.sponsor_id)).toEqual(before)
  })
})

describe('SponsorCardList filters', () => {
  it('searches name, organization and email', async () => {
    const wrapper = mountList(fixture())
    const input = wrapper.get('[data-testid="sponsor-search"]')

    await input.setValue('arthur')
    expect(ids(wrapper)).toEqual([10])
    await input.setValue('golden')
    expect(ids(wrapper)).toEqual([10])
    await input.setValue('CY@CHURCH')
    expect(ids(wrapper)).toEqual([30])
    expect(count(wrapper)).toBe('1 of 5 shown')
  })

  it('filters by affiliation built from the distinct values present', async () => {
    const wrapper = mountList(fixture())
    const select = wrapper.get('[data-testid="sponsor-filter-affiliation"]')
    expect(select.findAll('option').map((o) => o.text())).toEqual(['any', 'Business', 'Church'])

    await select.setValue('Church')
    expect(ids(wrapper)).toEqual([20, 30])
    expect(count(wrapper)).toBe('2 of 5 shown')
  })

  it('filters by audit status, listing only statuses present, unset included', async () => {
    const wrapper = mountList(fixture())
    const select = wrapper.get('[data-testid="sponsor-filter-status"]')
    expect(select.findAll('option').map((o) => o.text())).toEqual([
      'any',
      'new',
      'needs clarification',
      'audited',
      'unset',
    ])

    await select.setValue('audited')
    expect(ids(wrapper)).toEqual([10, 40])
    await select.setValue('unset')
    expect(ids(wrapper)).toEqual([50])
  })

  it('filters by availability: capacity, full, hide locked', async () => {
    const wrapper = mountList(fixture())
    const select = wrapper.get('[data-testid="sponsor-filter-availability"]')

    await select.setValue('capacity')
    expect(ids(wrapper)).toEqual([20, 30, 50])
    await select.setValue('full')
    expect(ids(wrapper)).toEqual([10, 40])
    await select.setValue('unlocked')
    expect(ids(wrapper)).toEqual([20, 30, 50])
  })

  it('hides locked rows when a dispatched sponsor still has capacity', async () => {
    const wrapper = mountList([
      sponsor({ sponsor_id: 1, packets_dispatched: true, remaining: 2 }),
      sponsor({ sponsor_id: 2 }),
    ])
    await wrapper.get('[data-testid="sponsor-filter-availability"]').setValue('unlocked')
    expect(ids(wrapper)).toEqual([2])
  })

  it('composes filters with the sort order', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-filter-affiliation"]').setValue('Business')
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('id-desc')
    expect(ids(wrapper)).toEqual([40, 10])
    await wrapper.get('[data-testid="sponsor-filter-status"]').setValue('audited')
    await wrapper.get('[data-testid="sponsor-filter-availability"]').setValue('unlocked')
    expect(wrapper.find('[data-testid="sponsor-no-match"]').exists()).toBe(true)
  })

  it('shows the count always and a clear button only while a filter is active', async () => {
    const wrapper = mountList(fixture())
    expect(count(wrapper)).toBe('5 of 5 shown')
    expect(wrapper.find('[data-testid="sponsor-clear-filters"]').exists()).toBe(false)

    await wrapper.get('[data-testid="sponsor-search"]').setValue('zed')
    await wrapper.get('[data-testid="sponsor-filter-status"]').setValue('needs_clarification')
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('id-desc')
    expect(ids(wrapper)).toEqual([20])

    await wrapper.get('[data-testid="sponsor-clear-filters"]').trigger('click')
    expect(count(wrapper)).toBe('5 of 5 shown')
    expect(wrapper.find('[data-testid="sponsor-clear-filters"]').exists()).toBe(false)
    expect((wrapper.get('[data-testid="sponsor-search"]').element as HTMLInputElement).value).toBe(
      '',
    )
    // Clearing filters leaves the chosen sort alone.
    expect(ids(wrapper)).toEqual([50, 40, 30, 20, 10])
  })

  it('shows a no-match state with its own clear button', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-search"]').setValue('nobody at all')

    expect(wrapper.get('[data-testid="sponsor-no-match"]').text()).toContain(
      'No sponsors match these filters.',
    )
    expect(wrapper.find('[data-testid="sponsor-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="sponsor-empty"]').exists()).toBe(false)
    expect(count(wrapper)).toBe('0 of 5 shown')

    await wrapper.get('[data-testid="sponsor-no-match-clear"]').trigger('click')
    expect(ids(wrapper)).toHaveLength(5)
    expect(wrapper.find('[data-testid="sponsor-no-match"]').exists()).toBe(false)
  })

  it('shows a distinct empty state, without controls, when there are no sponsors', () => {
    const wrapper = mountList([])
    expect(wrapper.get('[data-testid="sponsor-empty"]').text()).toContain('No sponsors to show.')
    expect(wrapper.find('[data-testid="sponsor-no-match"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="sponsor-search"]').exists()).toBe(false)
  })

  it('still emits a selection from a filtered, re-sorted list', async () => {
    const wrapper = mountList(fixture())
    await wrapper.get('[data-testid="sponsor-filter-affiliation"]').setValue('Church')
    await wrapper.get('[data-testid="sponsor-sort"]').setValue('id-desc')
    await wrapper.findAll('[data-testid="sponsor-row-radio"]')[0]!.setValue(true)
    expect(wrapper.emitted('update:modelValue')).toEqual([[30]])
  })
})
