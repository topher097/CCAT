<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

const props = defineProps<{
  /** The section name, e.g. "Children". */
  label: string
  /** The route the card links to, e.g. "/audit/children". */
  to: string
  /** How many rows the section holds in total. */
  total: number
  /** How many of them are finished (audited). Drives the progress bar. */
  done: number
  /** The finished bucket's label, e.g. "audited". */
  doneLabel: string
  /** The breakdown shown under the bar. */
  statuses: { key: string; label: string; count: number }[]
  /** Optional one-line footnote, e.g. "3 ignored · 1 orphan". */
  note?: string
}>()

const pct = computed(() => {
  if (props.total <= 0) return 0
  return Math.round((props.done / props.total) * 100)
})

const accessibleName = computed(() => `${props.label}, ${props.total} total`)
</script>

<template>
  <RouterLink
    :to="to"
    class="card audit-nav-card"
    :aria-label="accessibleName"
  >
    <span class="audit-nav-card-label">{{ label }}</span>
    <span class="audit-nav-card-total" aria-hidden="true">{{ total }}</span>

    <span class="audit-nav-card-progress">
      <span class="audit-nav-card-progress-track" aria-hidden="true">
        <span
          class="audit-nav-card-progress-fill"
          :style="{ width: `${pct}%` }"
        />
      </span>
      <span
        class="audit-nav-card-progress-text"
        role="progressbar"
        :aria-valuenow="done"
        aria-valuemin="0"
        :aria-valuemax="total"
      >
        {{ done }} {{ doneLabel }} of {{ total }}
        <span class="muted">({{ pct }}%)</span>
      </span>
    </span>

    <ul v-if="statuses.length" class="audit-nav-card-breakdown">
      <li
        v-for="status in statuses"
        :key="status.key"
        class="audit-nav-card-breakdown-item"
      >
        <span class="audit-nav-card-breakdown-label">{{ status.label }}</span>
        <span class="badge audit-nav-card-breakdown-count">{{ status.count }}</span>
      </li>
    </ul>

    <span v-if="note" class="audit-nav-card-note">{{ note }}</span>
  </RouterLink>
</template>
