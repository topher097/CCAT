<script setup lang="ts">
/** One child's overview line on a family card (plan §7.3). */

import { computed } from 'vue'

import type { ManualMatchChildCard } from '../api/manual-match'

const props = defineProps<{
  child: ManualMatchChildCard
  selected: boolean
  warnings?: string[]
  familyId?: number | null
}>()

const emit = defineEmits<{ toggle: [id: string]; openDetails: [id: string] }>()

const name = computed(() => props.child.first_name ?? 'Unnamed child')

const ageText = computed(() =>
  props.child.age_at_xmas === null || props.child.age_at_xmas === undefined
    ? 'age unknown'
    : `age ${props.child.age_at_xmas}`,
)

const selectLabel = computed(() => `Select ${name.value}, ${ageText.value}`)

const needs = computed(() =>
  [props.child.need_1, props.child.need_2].filter(Boolean).join(', '),
)

const wants = computed(() =>
  [props.child.desire_1, props.child.desire_2].filter(Boolean).join(', '),
)

const sizes = computed(() =>
  Object.entries(props.child.sizes ?? {})
    .filter(([, value]) => Boolean(value))
    .map(([key, value]) => `${key} ${value}`)
    .join(', '),
)

// Classes come from base.css (.badge.status-*); unknown values stay neutral.
const KNOWN_STATUSES = ['new', 'in_progress', 'needs_clarification', 'ignored', 'audited']

const auditLabel = computed(() =>
  props.child.status ? props.child.status.replace(/_/g, ' ') : 'unset',
)

const auditClass = computed(() =>
  props.child.status && KNOWN_STATUSES.includes(props.child.status)
    ? `status-${props.child.status.replace(/_/g, '-')}`
    : 'audit-unset',
)
</script>

<template>
  <li class="child-summary" :data-child-id="child.zite_record_id">
    <div class="child-head">
      <input
        :id="`select-${child.zite_record_id}`"
        type="checkbox"
        :checked="selected"
        :disabled="!child.selectable"
        :aria-label="selectLabel"
        @change="emit('toggle', child.zite_record_id)"
      />
      <h3>
        <label :for="`select-${child.zite_record_id}`">
          {{ name }}, {{ ageText }}, {{ child.gender ?? 'gender unknown' }}
        </label>
      </h3>
      <span
        v-if="familyId !== null && familyId !== undefined"
        class="family-tag"
        data-testid="child-family-id"
      >
        family #{{ familyId }}
      </span>
      <span class="badge">{{ child.angel_id ?? 'no angel id' }}</span>
      <span
        class="badge"
        :class="auditClass"
        data-testid="child-audit-status"
        :title="`Audit status: ${auditLabel}`"
      >
        audit: {{ auditLabel }}
      </span>
      <span v-if="child.special_needs" class="badge check-warn">special needs</span>
      <span
        v-if="child.assigned_sponsor_id !== null && child.assigned_sponsor_id !== undefined"
        class="badge check-ok"
      >
        sponsor {{ child.assigned_sponsor_id }}
      </span>
      <button
        type="button"
        class="details-button"
        data-testid="open-child-details"
        :aria-label="`View child details for ${name}`"
        @click="emit('openDetails', child.zite_record_id)"
      >
        Details ↗
      </button>
    </div>

    <!-- Label column + value column: aligned for scanning, wraps only inside the value. -->
    <dl class="child-details">
      <dt>Needs</dt>
      <dd :class="{ empty: !needs }">{{ needs || '—' }}</dd>
      <dt>Wants</dt>
      <dd :class="{ empty: !wants }">{{ wants || '—' }}</dd>
      <dt>Interests</dt>
      <dd :class="{ empty: !child.interests }">{{ child.interests || '—' }}</dd>
      <dt>Notes</dt>
      <dd :class="{ empty: !child.other_notes }">{{ child.other_notes || '—' }}</dd>
      <template v-if="child.favorite_colors">
        <dt>Fav. colors</dt>
        <dd>{{ child.favorite_colors }}</dd>
      </template>
      <template v-if="sizes">
        <dt>Sizes</dt>
        <dd>{{ sizes }}</dd>
      </template>
    </dl>

    <p
      v-if="!child.selectable && child.unselectable_reason"
      class="error-text"
      data-testid="unselectable-reason"
    >
      ⚠ {{ child.unselectable_reason }}
    </p>
    <ul v-if="warnings && warnings.length" class="child-warnings">
      <li v-for="warning in warnings" :key="warning">⚠ {{ warning }}</li>
    </ul>
  </li>
</template>

<style scoped>
.child-summary {
  border-top: 1px solid #e3e9ef;
  padding: 0.5rem 0;
}

.child-head {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: baseline;
}

.child-head h3 {
  margin: 0;
  font-size: 0.95rem;
}

.child-summary p {
  margin: 0.2rem 0;
}

/* Plain monospace text, not a pill: reads as an ID next to the status badges. */
.family-tag {
  font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
  font-size: 0.8rem;
  font-weight: 700;
  color: #12324f;
  white-space: nowrap;
}

.details-button {
  margin-left: auto;
  padding: 0.05rem 0.5rem;
  font-size: 0.78rem;
  border-radius: 999px;
  color: #0b5cad;
  white-space: nowrap;
}

.details-button:hover {
  background: #e8f0fa;
}

.badge.audit-unset {
  border-style: dashed;
  color: #5c6b7a;
  background: transparent;
}

.child-details {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 0.15rem 0.75rem;
  margin: 0.3rem 0;
  font-size: 0.88rem;
  line-height: 1.35;
}

.child-details dt {
  color: #5c6b7a;
  font-size: 0.74rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding-top: 0.12rem;
}

.child-details dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.child-details dd.empty {
  color: #8a96a3;
}

.child-warnings {
  margin: 0.2rem 0 0;
  padding-left: 1.1rem;
  color: #9a5b12;
}
</style>
