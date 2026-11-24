<script setup lang="ts">
/**
 * Filterable, sortable single-select sponsor list for manual matching, with a
 * dispatch link per row. Filtering and sorting are client-side over `sponsors`.
 */

import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  organization,
  personName,
  remainingOf,
} from '../utils/sponsor'

/**
 * The sponsor fields the rows read. Structural on purpose: the generated
 * `ManualMatchSponsor` type gains the newer fields on the next API regen.
 */
export type SponsorCard = {
  sponsor_id: number
  row_version: number
  display_name?: string | null
  first_name?: string | null
  last_name?: string | null
  organization_name?: string | null
  email?: string | null
  phone?: string | null
  sponsor_type?: string | null
  num_children?: number | null
  assigned_count?: number | null
  remaining?: number | null
  age_preference?: string | null
  gender_preference?: string | null
  dropoff_location?: string | null
  pref_age_min?: number | null
  pref_age_max?: number | null
  status?: string | null
  elevated_budget?: boolean | null
  physical_packet?: boolean | null
  packets_dispatched?: boolean
  packets_dispatched_at?: string | null
}

const props = defineProps<{
  sponsors: SponsorCard[]
  modelValue: number | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: number | null]
  openDetails: [sponsorId: number]
  openAssignments: [sponsorId: number]
}>()

// ---- display helpers -------------------------------------------------------

// Classes come from base.css (.badge.status-*); unknown values stay neutral.
const KNOWN_STATUSES = ['new', 'in_progress', 'needs_clarification', 'ignored', 'audited']

function statusText(status: string | null | undefined): string {
  return status ? status.replace(/_/g, ' ') : 'unset'
}

function auditClass(sponsor: SponsorCard): string {
  return sponsor.status && KNOWN_STATUSES.includes(sponsor.status)
    ? `status-${sponsor.status.replace(/_/g, '-')}`
    : 'audit-unset'
}

function isFull(sponsor: SponsorCard): boolean {
  return remainingOf(sponsor) === 0
}

function isDispatched(sponsor: SponsorCard): boolean {
  return sponsor.packets_dispatched === true
}

/** Locked = cannot be chosen: full or already dispatched. */
function isLocked(sponsor: SponsorCard): boolean {
  return isFull(sponsor) || isDispatched(sponsor)
}

function wantedText(sponsor: SponsorCard): string {
  return sponsor.num_children === null || sponsor.num_children === undefined
    ? 'unlimited'
    : String(sponsor.num_children)
}

function remainingText(sponsor: SponsorCard): string {
  const remaining = remainingOf(sponsor)
  if (remaining === null) return 'unlimited'
  return remaining === 0 ? 'full' : String(remaining)
}

function radioLabel(sponsor: SponsorCard): string {
  const remaining = remainingOf(sponsor)
  const capacity =
    remaining === null
      ? 'unlimited'
      : remaining === 0
        ? 'full'
        : `${remaining} of ${sponsor.num_children} remaining`
  const name = sponsor.display_name || personName(sponsor)
  return `${name}, ${capacity}${isDispatched(sponsor) ? ', dispatched' : ''}`
}

function ageText(sponsor: SponsorCard): string {
  const { pref_age_min: min, pref_age_max: max } = sponsor
  const hasMin = min !== null && min !== undefined
  const hasMax = max !== null && max !== undefined
  const range = hasMin && hasMax ? `${min}–${max}` : hasMin ? `${min}+` : hasMax ? `up to ${max}` : ''
  if (sponsor.age_preference && range) return `${sponsor.age_preference} (${range})`
  return sponsor.age_preference || range
}

// ---- filters ---------------------------------------------------------------

type Availability = 'any' | 'capacity' | 'full' | 'unlocked'
type SortKey = 'id-asc' | 'id-desc' | 'name' | 'remaining-most' | 'remaining-least'

const search = ref('')
const affiliation = ref('')
const status = ref('')
const availability = ref<Availability>('any')
const sort = ref<SortKey>('id-asc')

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'id-asc', label: 'Sponsor # ↑' },
  { value: 'id-desc', label: 'Sponsor # ↓' },
  { value: 'name', label: 'Name A–Z' },
  { value: 'remaining-most', label: 'Remaining ↑ (most first)' },
  { value: 'remaining-least', label: 'Remaining ↓ (fewest first)' },
]

const AVAILABILITY_OPTIONS: { value: Availability; label: string }[] = [
  { value: 'any', label: 'any' },
  { value: 'capacity', label: 'has capacity' },
  { value: 'full', label: 'full' },
  { value: 'unlocked', label: 'hide locked (full or dispatched)' },
]

/** Distinct, non-empty affiliations present in the list. */
const affiliations = computed(() =>
  [...new Set(props.sponsors.map((s) => s.sponsor_type).filter((t): t is string => Boolean(t)))].sort(
    (a, b) => a.localeCompare(b),
  ),
)

/** Statuses present in the list, in the audit order; unset gets its own entry. */
const statuses = computed(() => {
  const present = new Set(props.sponsors.map((s) => s.status ?? ''))
  const known = KNOWN_STATUSES.filter((s) => present.has(s))
  const other = [...present].filter((s) => s !== '' && !KNOWN_STATUSES.includes(s)).sort()
  return [...known, ...other, ...(present.has('') ? [''] : [])]
})

const filtersActive = computed(
  () =>
    search.value.trim() !== '' ||
    affiliation.value !== '' ||
    status.value !== '' ||
    availability.value !== 'any',
)

function clearFilters(): void {
  search.value = ''
  affiliation.value = ''
  status.value = ''
  availability.value = 'any'
}

function matches(sponsor: SponsorCard): boolean {
  const needle = search.value.trim().toLowerCase()
  if (needle) {
    const haystack = [
      personName(sponsor),
      sponsor.display_name,
      sponsor.organization_name,
      sponsor.email,
    ]
      .filter(Boolean)
      .join('\n')
      .toLowerCase()
    if (!haystack.includes(needle)) return false
  }
  if (affiliation.value && sponsor.sponsor_type !== affiliation.value) return false
  // '' means "any" for the select; the unset status is filtered as 'unset'.
  if (status.value) {
    const own = sponsor.status || 'unset'
    if (own !== status.value) return false
  }
  if (availability.value === 'capacity' && isFull(sponsor)) return false
  if (availability.value === 'full' && !isFull(sponsor)) return false
  if (availability.value === 'unlocked' && isLocked(sponsor)) return false
  return true
}

/** Sort key for capacity: unlimited sponsors count as the most room. */
function room(sponsor: SponsorCard): number {
  return remainingOf(sponsor) ?? Number.POSITIVE_INFINITY
}

function compare(a: SponsorCard, b: SponsorCard): number {
  const byId = a.sponsor_id - b.sponsor_id
  switch (sort.value) {
    case 'id-desc':
      return -byId
    case 'name':
      return personName(a).localeCompare(personName(b)) || byId
    case 'remaining-most':
      return room(b) === room(a) ? byId : room(b) > room(a) ? 1 : -1
    case 'remaining-least':
      return room(a) === room(b) ? byId : room(a) > room(b) ? 1 : -1
    default:
      return byId
  }
}

const visible = computed(() => props.sponsors.filter(matches).sort(compare))
</script>

<template>
  <div class="sponsor-list" data-testid="sponsor-list">
    <p v-if="!sponsors.length" class="muted empty" data-testid="sponsor-empty">
      No sponsors to show.
    </p>

    <template v-else>
      <div class="controls">
        <input
          v-model="search"
          type="search"
          name="sponsor-search"
          placeholder="Search sponsor name, organization, email"
          aria-label="Search sponsors"
          data-testid="sponsor-search"
        />
        <label class="muted">
          Affiliation
          <select
            v-model="affiliation"
            aria-label="Filter by affiliation"
            data-testid="sponsor-filter-affiliation"
          >
            <option value="">any</option>
            <option v-for="type in affiliations" :key="type" :value="type">{{ type }}</option>
          </select>
        </label>
        <label class="muted">
          Audit
          <select v-model="status" aria-label="Filter by audit status" data-testid="sponsor-filter-status">
            <option value="">any</option>
            <option v-for="value in statuses" :key="value" :value="value || 'unset'">
              {{ statusText(value) }}
            </option>
          </select>
        </label>
        <label class="muted">
          Availability
          <select
            v-model="availability"
            aria-label="Filter by availability"
            data-testid="sponsor-filter-availability"
          >
            <option v-for="option in AVAILABILITY_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <label class="muted">
          Sort
          <select v-model="sort" aria-label="Sort sponsors" data-testid="sponsor-sort">
            <option v-for="option in SORT_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <span class="muted count" role="status" data-testid="sponsor-count">
          {{ visible.length }} of {{ sponsors.length }} shown
        </span>
        <button
          v-if="filtersActive"
          type="button"
          data-testid="sponsor-clear-filters"
          @click="clearFilters"
        >
          Clear filters
        </button>
      </div>

      <div v-if="!visible.length" class="muted empty" data-testid="sponsor-no-match">
        No sponsors match these filters.
        <button type="button" data-testid="sponsor-no-match-clear" @click="clearFilters">
          Clear filters
        </button>
      </div>

      <div v-else class="rows" role="radiogroup" aria-label="Sponsors">
        <div
          v-for="sponsor in visible"
          :key="sponsor.sponsor_id"
          class="row"
          :class="{
            selected: modelValue === sponsor.sponsor_id,
            unavailable: isLocked(sponsor),
          }"
          data-testid="sponsor-row"
          :data-sponsor-id="sponsor.sponsor_id"
        >
          <input
            :id="`sponsor-shop-${sponsor.sponsor_id}`"
            class="radio"
            type="radio"
            name="sponsor-shop"
            :value="sponsor.sponsor_id"
            :checked="modelValue === sponsor.sponsor_id"
            :disabled="disabled || isLocked(sponsor)"
            :aria-label="radioLabel(sponsor)"
            data-testid="sponsor-row-radio"
            @change="emit('update:modelValue', sponsor.sponsor_id)"
          />
          <span class="sponsor-id" data-testid="sponsor-row-id">#{{ sponsor.sponsor_id }}</span>

          <label :for="`sponsor-shop-${sponsor.sponsor_id}`" class="who dim">
            <strong class="name">{{ personName(sponsor) }}</strong>
            <span v-if="organization(sponsor)" class="org">{{ organization(sponsor) }}</span>
            <span v-if="sponsor.sponsor_type" class="org">{{ sponsor.sponsor_type }}</span>
            <span v-if="sponsor.email || sponsor.phone" class="contact">
              <span
                v-if="sponsor.email"
                class="email"
                :title="sponsor.email"
                data-testid="sponsor-row-email"
              >
                {{ sponsor.email }}
              </span>
              <span v-if="sponsor.phone" class="phone" data-testid="sponsor-row-phone">
                {{ sponsor.phone }}
              </span>
            </span>
          </label>

          <span class="badges">
            <span
              class="badge"
              :class="auditClass(sponsor)"
              :title="`Audit status: ${statusText(sponsor.status)}`"
              data-testid="sponsor-row-status"
            >
              audit: {{ statusText(sponsor.status) }}
            </span>
            <span
              v-if="isDispatched(sponsor)"
              class="badge status-audited"
              data-testid="sponsor-row-dispatched"
            >
              Dispatched
            </span>
            <span v-if="isFull(sponsor)" class="badge status-ignored" data-testid="sponsor-row-full">
              full
            </span>
            <span v-if="sponsor.elevated_budget" class="flag" data-testid="sponsor-row-elevated">
              elevated budget
            </span>
            <span v-if="sponsor.physical_packet" class="flag" data-testid="sponsor-row-physical">
              physical packet
            </span>
          </span>

          <span class="counts dim">
            <span><span class="label">Wanted</span> {{ wantedText(sponsor) }}</span>
            <span><span class="label">Assigned</span> <button
              type="button"
              class="assigned-trigger"
              data-testid="assigned-angels-trigger"
              :aria-label="`View assigned angels for ${personName(sponsor)}, ${sponsor.assigned_count ?? 0}`"
              @click.stop="emit('openAssignments', sponsor.sponsor_id)"
            >
              {{ sponsor.assigned_count ?? 0 }}
            </button></span>
            <span><span class="label">Left</span> {{ remainingText(sponsor) }}</span>
          </span>

          <span class="prefs dim">
            <span :class="{ empty: !ageText(sponsor) }">
              <span class="label">Age</span> {{ ageText(sponsor) || '—' }}
            </span>
            <span :class="{ empty: !sponsor.gender_preference }">
              <span class="label">Gender</span> {{ sponsor.gender_preference || '—' }}
            </span>
            <span :class="{ empty: !sponsor.dropoff_location }">
              <span class="label">Drop-off</span> {{ sponsor.dropoff_location || '—' }}
            </span>
          </span>

          <span class="actions">
            <button
              type="button"
              class="details"
              data-testid="open-sponsor-details"
              :aria-label="`View sponsor ${personName(sponsor)} details`"
              title="View sponsor details"
              @click="emit('openDetails', sponsor.sponsor_id)"
            >
              ↗
            </button>
            <RouterLink
              :to="{ path: '/dispatch', query: { sponsor: sponsor.sponsor_id } }"
              class="dispatch"
              data-testid="dispatch-packets"
            >
              View dispatch
            </RouterLink>
          </span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sponsor-list {
  flex: 1 1 100%;
  min-width: 0;
}

.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 0.75rem;
  align-items: center;
  margin-bottom: 0.6rem;
}

.controls input[type='search'] {
  flex: 1 1 14rem;
  min-width: 10rem;
}

.controls label {
  display: inline-flex;
  gap: 0.3rem;
  align-items: center;
  font-size: 0.88rem;
}

.count {
  margin-left: auto;
  font-size: 0.88rem;
}

.empty {
  margin: 0.4rem 0;
}

.rows {
  display: flex;
  flex-direction: column;
  max-height: 24rem;
  overflow-y: auto;
  background: #fff;
  border: 1px solid #d7dee6;
  border-radius: 6px;
}

/* One shared column template, so columns line up from row to row. */
.row {
  display: grid;
  grid-template-columns:
    1.2rem 3.6rem minmax(9rem, 1.3fr) minmax(8rem, 1.1fr)
    minmax(11rem, 1fr) minmax(10rem, 1.2fr) max-content;
  gap: 0.2rem 0.75rem;
  align-items: center;
  padding: 0.45rem 0.7rem;
  border-top: 1px solid #e3e9ef;
  font-size: 0.86rem;
}

.row:first-child {
  border-top: 0;
}

.row:focus-within {
  outline: 2px solid #0b5cad;
  outline-offset: -2px;
}

.row.selected {
  background: #f1f7fd;
  box-shadow: inset 3px 0 0 #0b5cad;
}

/* Badges and the dispatch button stay at full strength; only the details dim. */
.row.unavailable {
  background: #f1f4f7;
}

.row.unavailable .dim {
  opacity: 0.6;
}

.radio {
  margin: 0;
}

/* Same family as FamilyCard's #id chip, but not a link, so no arrow. */
.sponsor-id {
  justify-self: start;
  padding: 0.05rem 0.45rem;
  border-radius: 4px;
  background: #12324f;
  color: #fff;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
  font-size: 0.85rem;
  font-weight: 700;
}

.who {
  display: flex;
  flex-direction: column;
  min-width: 0;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.name {
  font-size: 0.95rem;
}

/* Email truncates with an ellipsis (full address in the title); phone never wraps. */
.contact {
  display: flex;
  gap: 0.2rem 0.6rem;
  min-width: 0;
  color: #5c6b7a;
  font-size: 0.8rem;
}

.email {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.phone {
  white-space: nowrap;
}

.org {
  color: #5c6b7a;
  font-size: 0.8rem;
}

.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  align-items: center;
}

.badge.audit-unset {
  border-style: dashed;
  color: #5c6b7a;
  background: transparent;
}

/* Quieter than the audit badge: smaller, no fill, light border. */
.flag {
  padding: 0 0.4rem;
  border: 1px solid #c3ccd6;
  border-radius: 999px;
  color: #3d4b59;
  font-size: 0.72rem;
  white-space: nowrap;
}

.counts,
.prefs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.1rem 0.8rem;
  min-width: 0;
}

.prefs > span {
  overflow-wrap: anywhere;
}

.prefs .empty {
  color: #8a96a3;
}

.label {
  color: #5c6b7a;
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.assigned-trigger {
  padding: 0;
  border: 0;
  border-bottom: 1px dotted #0b5cad;
  border-radius: 0;
  background: transparent;
  color: #0b5cad;
  font: inherit;
  font-weight: 700;
}

.assigned-trigger:hover {
  border-bottom-style: solid;
  background: #e8f0fa;
}

.assigned-trigger:focus-visible {
  outline: 2px solid #0b5cad;
  outline-offset: 2px;
}

.actions {
  display: flex;
  gap: 0.4rem;
  align-items: center;
}

/* Same small arrow as FamilyCard / ChildSummary: opens the full editor. */
.details {
  padding: 0.05rem 0.45rem;
  border-radius: 999px;
  color: #0b5cad;
  font-size: 0.85rem;
}

.details:hover {
  background: #e8f0fa;
}

.dispatch {
  white-space: nowrap;
  font-size: 0.82rem;
}

@media (max-width: 1100px) {
  .row {
    grid-template-columns: 1.2rem 3.6rem minmax(0, 1fr) max-content;
  }

  .badges,
  .counts,
  .prefs {
    grid-column: 3 / -1;
  }

  .actions {
    grid-column: 4;
    grid-row: 1;
  }
}
</style>
