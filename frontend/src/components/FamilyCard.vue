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
}>()

const STATE_LABELS: Record<string, string> = {
  unassigned: 'Unassigned',
  partial: 'Partial',
  split: 'Split',
  assigned: 'Assigned',
}

const children = computed(() => props.card.children ?? [])
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
        <h2>{{ contact }}</h2>
        <span class="badge" data-testid="assignment-state">
          {{ stateLabel }}
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
        @toggle="emit('toggle', $event)"
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
