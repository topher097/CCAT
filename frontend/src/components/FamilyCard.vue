<script setup lang="ts">
/** One family card: header, children, select-all and the hold section. */

import { computed } from 'vue'

import type { ManualMatchFamilyCard } from '../api/manual-match'
import { formatTime } from '../utils/format'
import ChildSummary from './ChildSummary.vue'

const props = defineProps<{
  card: ManualMatchFamilyCard
  selected: Set<string>
}>()

const emit = defineEmits<{
  toggle: [id: string]
  selectAll: [familyId: number]
  releaseHold: [familyId: number]
  openDetails: [familyId: number]
  openChildDetails: [childId: string]
}>()

const STATE_LABELS: Record<string, string> = {
  unassigned: 'Unassigned',
  partial: 'Partial',
  split: 'Split',
  assigned: 'Assigned',
}

/** Angel id as letter + number ("B10"): letter first, then the number, so B2 < B10. */
function angelKey(id: string | null | undefined): [string, number] | null {
  const match = /^\s*([A-Za-z]*)\s*(\d*)/.exec(id ?? '')
  return id && match ? [match[1]!.toUpperCase(), Number(match[2] || 0)] : null
}

// Sorted copy ordered by angel id; children without one go last (sort is stable).
const children = computed(() =>
  [...(props.card.children ?? [])].sort((a, b) => {
    const ka = angelKey(a.angel_id)
    const kb = angelKey(b.angel_id)
    if (!ka || !kb) return ka ? -1 : kb ? 1 : 0
    return ka[0].localeCompare(kb[0]) || ka[1] - kb[1]
  }),
)
const selectable = computed(() => children.value.filter((child) => child.selectable))
const allSelected = computed(
  () =>
    selectable.value.length > 0 &&
    selectable.value.every((child) => props.selected.has(child.zite_record_id)),
)

const contact = computed(
  () =>
    [props.card.first_name, props.card.last_name].filter(Boolean).join(' ') ||
    `Family ${props.card.family_id}`,
)

const place = computed(() =>
  [props.card.city, props.card.state].filter(Boolean).join(', '),
)

const stateLabel = computed(
  () => STATE_LABELS[props.card.assignment_state] ?? props.card.assignment_state,
)

// Classes come from base.css (.badge.status-*); unknown values stay neutral.
const KNOWN_STATUSES = ['new', 'in_progress', 'needs_clarification', 'ignored', 'audited']

const auditLabel = computed(() =>
  props.card.status ? props.card.status.replace(/_/g, ' ') : 'unset',
)

const auditClass = computed(() =>
  props.card.status && KNOWN_STATUSES.includes(props.card.status)
    ? `status-${props.card.status.replace(/_/g, '-')}`
    : 'audit-unset',
)

const hold = computed(() =>
  props.card.hold?.held ? props.card.hold : null,
)
</script>

<template>
  <article
    class="card family-card"
    data-testid="family-card"
    :data-assignment-state="card.assignment_state"
  >
    <header>
      <div class="status-line" style="justify-content: space-between">
        <div class="status-line" style="gap: 0.5rem">
          <button
            type="button"
            class="family-id"
            data-testid="open-family-details"
            :aria-label="`View family ${card.family_id} details`"
            title="View family details"
            @click="emit('openDetails', card.family_id)"
          >
            #{{ card.family_id }} <span aria-hidden="true">↗</span>
          </button>
          <h2>{{ contact }}</h2>
        </div>
        <span class="status-line" style="gap: 0.4rem">
          <span
            class="badge"
            :class="auditClass"
            data-testid="audit-status"
            :title="`Audit status: ${auditLabel}`"
          >
            audit: {{ auditLabel }}
          </span>
          <span class="badge" data-testid="assignment-state">
            {{ stateLabel }}
          </span>
        </span>
      </div>
      <p class="muted family-meta">
        {{ card.gift_pickup_location || 'location unknown' }} ·
        {{ card.family_size }} children · {{ card.assigned_count }} assigned
        <span v-if="card.hold?.held" class="badge" data-testid="held-badge">
          Held
        </span>
      </p>
      <p class="muted family-meta">
        {{ card.primary_email || '—' }} · {{ card.phone || '—' }} ·
        {{ place || '—' }}
      </p>
    </header>

    <section v-if="hold" class="hold" data-testid="hold-section">
      <p>
        ⛔ Held by {{ hold.created_by || 'unknown' }},
        {{ formatTime(hold.created_at) }}: “{{ hold.note }}”
      </p>
      <button
        type="button"
        data-testid="release-hold"
        @click="emit('releaseHold', card.family_id)"
      >
        Release hold
      </button>
    </section>

    <div class="status-line" style="margin-top: 0.5rem">
      <button
        type="button"
        data-testid="select-all"
        :disabled="selectable.length === 0"
        @click="emit('selectAll', card.family_id)"
      >
        {{ allSelected ? 'All children selected' : `Select all ${selectable.length}` }}
      </button>
      <span class="muted">
        {{ selectable.filter((c) => selected.has(c.zite_record_id)).length }} of
        {{ selectable.length }} selectable selected
      </span>
    </div>

    <ul v-if="children.length" class="children">
      <ChildSummary
        v-for="child in children"
        :key="child.zite_record_id"
        :child="child"
        :selected="selected.has(child.zite_record_id)"
        :family-id="card.family_id"
        @toggle="emit('toggle', $event)"
        @open-details="(id: string) => emit('openChildDetails', id)"
      />
    </ul>
    <p v-else class="muted">This family has no children in the pool.</p>

    <ul
      v-if="card.warnings && card.warnings.length"
      class="family-warnings"
      data-testid="family-warnings"
    >
      <li v-for="warning in card.warnings" :key="warning">⚠ {{ warning }}</li>
    </ul>
  </article>
</template>

<style scoped>
.family-card {
  display: flex;
  flex-direction: column;
}

.family-card h2 {
  margin: 0;
  font-size: 1.05rem;
}

.family-meta {
  margin: 0.15rem 0;
}

/* The family number: square, dark and monospaced so it reads as an ID, not a status. */
.family-id {
  padding: 0.1rem 0.5rem;
  border-color: #12324f;
  border-radius: 4px;
  background: #12324f;
  color: #fff;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
  font-size: 0.9rem;
  font-weight: 700;
}

.family-id:hover {
  background: #1d4d78;
}

.family-id:focus-visible {
  outline: 2px solid #0b5cad;
  outline-offset: 2px;
}

.badge.audit-unset {
  border-style: dashed;
  color: #5c6b7a;
  background: transparent;
}

.children {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
}

.hold {
  border: 1px solid #9e2b25;
  border-radius: 4px;
  background: #f8dedc;
  padding: 0.4rem 0.6rem;
  margin-top: 0.5rem;
}

.hold p {
  margin: 0 0 0.4rem;
}

.family-warnings {
  margin: 0.4rem 0 0;
  padding-left: 1.1rem;
  color: #9a5b12;
}
</style>
