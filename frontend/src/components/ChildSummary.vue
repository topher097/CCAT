<script setup lang="ts">
/** One child's overview line on a family card (plan §7.3). */

import { computed } from 'vue'

import type { ManualMatchChildCard } from '../api/manual-match'

const props = defineProps<{
  child: ManualMatchChildCard
  selected: boolean
  warnings?: string[]
}>()

const emit = defineEmits<{ toggle: [id: string] }>()

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
      <span class="badge">{{ child.angel_id ?? 'no angel id' }}</span>
      <span v-if="child.special_needs" class="badge">special needs</span>
      <span
        v-if="child.assigned_sponsor_id !== null && child.assigned_sponsor_id !== undefined"
        class="badge"
      >
        sponsor {{ child.assigned_sponsor_id }}
      </span>
    </div>

    <p class="muted">
      Needs: {{ needs || '—' }} · Wants: {{ wants || '—' }} ·
      Interests: {{ child.interests || '—' }} · Notes:
      {{ child.other_notes || '—' }}
    </p>
    <p v-if="child.favorite_colors" class="muted">
      Favourite colours: {{ child.favorite_colors }}
    </p>
    <p v-if="sizes" class="muted">Sizes: {{ sizes }}</p>

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

.child-warnings {
  margin: 0.2rem 0 0;
  padding-left: 1.1rem;
  color: #9a5b12;
}
</style>
