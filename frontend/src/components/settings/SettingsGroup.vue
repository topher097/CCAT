<script setup lang="ts">
/** A regular object: a card with a heading and one row per child key. */

import { computed } from 'vue'

import { useSettingsStore } from '../../stores/settings'
import { getAt, humanize, metaFor, type Path } from '../../settings/tree'
import SettingsNode from './SettingsNode.vue'

const props = defineProps<{
  path: Path
}>()

const store = useSettingsStore()

const value = computed<unknown>(() =>
  store.draft ? getAt(store.draft, props.path) : undefined,
)
const meta = computed(() => metaFor(props.path))
const label = computed(
  () => meta.value.label ?? humanize(props.path[props.path.length - 1] ?? ''),
)
const description = computed(() => meta.value.description)
const dotted = computed(() => props.path.join('.'))
const error = computed(() => store.errorAt(props.path))

const childKeys = computed<string[]>(() => {
  const v = value.value
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return []
  return Object.keys(v as Record<string, unknown>)
})
</script>

<template>
  <div
    class="settings-group card"
    :data-path="dotted"
    data-testid="settings-group"
  >
    <div class="group-header">
      <h3>{{ label }}</h3>
      <p v-if="description" class="muted">{{ description }}</p>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    </div>
    <div class="group-body">
      <SettingsNode
        v-for="key in childKeys"
        :key="`${dotted}-${key}`"
        :path="[...path, key]"
      />
    </div>
  </div>
</template>

<style scoped>
.settings-group {
  margin-bottom: 1rem;
}

.group-header {
  margin-bottom: 0.5rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid #e2e8ee;
}

.group-header h3 {
  margin: 0 0 0.2rem;
  font-size: 1.1rem;
  color: #12324f;
}

.group-header p {
  margin: 0;
}

.group-body > :deep(.settings-field) {
  border-bottom: 1px solid #f0f2f5;
}

.group-body > :deep(.settings-field:last-child) {
  border-bottom: none;
}
</style>
