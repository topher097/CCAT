<script setup lang="ts">
import { computed } from 'vue'

import type { ConflictInfo } from '../utils/conflict'
import { differs, normalizeValue } from '../utils/diff'
import { formatTime } from '../utils/format'

const props = defineProps<{
  conflict: ConflictInfo
  /** Draft values (all editables). */
  draft: Record<string, unknown>
  /** Field labels for the changed-field diff. */
  labels: Record<string, string>
  neutralFields?: readonly string[]
}>()

const emit = defineEmits<{ reload: []; rebase: [] }>()

/** Fields where the user's draft differs from the winning row. */
const changed = computed(() =>
  Object.keys(props.labels).filter(
    (field) => differs(props.draft[field], props.conflict.row[field]),
  ),
)

function show(value: unknown): string {
  const text = normalizeValue(value)
  return text || '—'
}
</script>

<template>
  <section
    class="card diff-changed"
    role="alert"
    data-testid="conflict-panel"
    style="border-color: #b05c12"
  >
    <h2 style="margin-top: 0">Save conflict — someone else changed this row</h2>
    <p>
      {{ conflict.message }} Your edits are still here; nothing was submitted.
    </p>
    <p class="muted">
      Fresh server version {{ conflict.currentVersion }} · last updated by
      <strong>{{ conflict.lastUpdatedBy ?? 'unknown' }}</strong>
      at {{ formatTime(conflict.lastUpdatedAt) }}
    </p>

    <table v-if="changed.length" class="data">
      <thead>
        <tr>
          <th>Field</th>
          <th>Fresh server value</th>
          <th>Your edit</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="field in changed"
          :key="field"
          :class="
            props.neutralFields?.includes(field) ? 'diff-neutral' : 'diff-changed'
          "
        >
          <td>{{ labels[field] }}</td>
          <td>{{ show(conflict.row[field]) }}</td>
          <td>{{ show(draft[field]) }}</td>
        </tr>
      </tbody>
    </table>

    <div class="dialog-actions">
      <button type="button" data-testid="conflict-reload" @click="emit('reload')">
        Reload (discard mine)
      </button>
      <button
        v-if="conflict.mode === 'edit'"
        type="button"
        class="primary"
        data-testid="conflict-rebase"
        @click="emit('rebase')"
      >
        Rebase onto fresh row
      </button>
    </div>
  </section>
</template>
