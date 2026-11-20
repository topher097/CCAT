<script setup lang="ts">
/** Dispatch one path to the right editor: scalar, list, map, or group. */

import { computed } from 'vue'

import { useSettingsStore } from '../../stores/settings'
import { getAt, metaFor, type Path } from '../../settings/tree'
import SettingsField from './SettingsField.vue'
import SettingsGroup from './SettingsGroup.vue'
import SettingsList from './SettingsList.vue'
import SettingsMap from './SettingsMap.vue'

const props = defineProps<{
  path: Path
}>()

const store = useSettingsStore()

const value = computed<unknown>(() =>
  store.draft ? getAt(store.draft, props.path) : undefined,
)

const kind = computed<'scalar' | 'array' | 'map' | 'object'>(() => {
  const v = value.value
  if (v === null || typeof v !== 'object') return 'scalar'
  if (Array.isArray(v)) return 'array'
  if (metaFor(props.path).map) return 'map'
  return 'object'
})
</script>

<template>
  <SettingsField v-if="kind === 'scalar'" :path="path" />
  <SettingsList v-else-if="kind === 'array'" :path="path" />
  <SettingsMap v-else-if="kind === 'map'" :path="path" />
  <SettingsGroup v-else :path="path" />
</template>
