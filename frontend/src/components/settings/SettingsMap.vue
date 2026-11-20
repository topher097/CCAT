<script setup lang="ts">
/** Map editor: entries with editable keys, add / delete, and nested values. */

import { computed, ref } from 'vue'

import { useSettingsStore } from '../../stores/settings'
import { getAt, humanize, metaFor, templateFor, type Path } from '../../settings/tree'
import SettingsNode from './SettingsNode.vue'

const props = defineProps<{
  path: Path
}>()

const store = useSettingsStore()

const entries = computed<[string, unknown][]>(() => {
  const v = store.draft ? getAt(store.draft, props.path) : undefined
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return []
  return Object.entries(v as Record<string, unknown>)
})

const meta = computed(() => metaFor(props.path))
const label = computed(
  () => meta.value.label ?? humanize(props.path[props.path.length - 1] ?? ''),
)
const description = computed(() => meta.value.description)
const dotted = computed(() => props.path.join('.'))
const error = computed(() => store.errorAt(props.path))

const isColumnMap = computed(() => meta.value.titleFrom === 'rename')

const adding = ref(false)
const newKey = ref('')

function entryPath(key: string): Path {
  return [...props.path, key]
}

function entryLabel(key: string, value: unknown): string {
  if (isColumnMap.value && value !== null && typeof value === 'object') {
    const rename = (value as Record<string, unknown>).rename
    if (typeof rename === 'string' && rename.length > 0) return rename
  }
  return humanize(key)
}

function remove(key: string): void {
  store.removeNode(entryPath(key))
}

function startAdd(): void {
  adding.value = true
  newKey.value = ''
}

function cancelAdd(): void {
  adding.value = false
  newKey.value = ''
}

function confirmAdd(): void {
  const key = newKey.value.trim()
  if (!key) return
  const values = entries.value.map(([, v]) => v)
  const template = templateFor(props.path, values)
  store.addEntry(props.path, key, template)
  adding.value = false
  newKey.value = ''
}

function rename(oldKey: string, event: Event): void {
  const input = event.target as HTMLInputElement
  const nextKey = input.value.trim()
  if (!nextKey || nextKey === oldKey) {
    input.value = oldKey
    return
  }
  store.renameKey(entryPath(oldKey), nextKey)
  // The store refuses a rename onto an existing key; put the old name
  // back so the field does not lie about the configuration.
  if (store.draft && getAt(store.draft, entryPath(oldKey)) !== undefined) {
    input.value = oldKey
  }
}
</script>

<template>
  <div
    class="settings-map"
    :data-path="dotted"
    data-testid="settings-map"
  >
    <div class="map-header">
      <div>
        <h4>{{ label }}</h4>
        <p v-if="description" class="muted">{{ description }}</p>
        <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      </div>
      <button
        v-if="!adding"
        type="button"
        class="primary"
        data-testid="settings-add-entry"
        :data-path="dotted"
        @click="startAdd"
      >
        Add
      </button>
    </div>

    <div v-if="adding" class="add-form card">
      <label :for="`${dotted}-new-key`">New key</label>
      <input
        :id="`${dotted}-new-key`"
        v-model="newKey"
        type="text"
        placeholder="new_key"
        data-testid="settings-new-key"
        @keydown.enter="confirmAdd"
      />
      <div class="add-actions">
        <button
          type="button"
          class="primary"
          data-testid="settings-confirm-add"
          @click="confirmAdd"
        >
          Add
        </button>
        <button
          type="button"
          data-testid="settings-cancel-add"
          @click="cancelAdd"
        >
          Cancel
        </button>
      </div>
    </div>

    <ul v-if="entries.length > 0" class="map-body">
      <li
        v-for="[key, value] in entries"
        :key="`${dotted}-${key}`"
        class="map-row card"
      >
        <div class="map-key">
          <label :for="`${dotted}-${key}-key`">Key</label>
          <input
            :id="`${dotted}-${key}-key`"
            type="text"
            :value="key"
            @change="rename(key, $event)"
          />
          <span class="display-title">{{ entryLabel(key, value) }}</span>
        </div>
        <div class="map-value">
          <SettingsNode :path="entryPath(key)" />
        </div>
        <button
          type="button"
          class="danger"
          data-testid="settings-delete"
          :data-path="entryPath(key).join('.')"
          data-kind="entry"
          @click="remove(key)"
        >
          Delete {{ key }}
        </button>
      </li>
    </ul>

    <p v-else class="muted empty">No entries yet.</p>
  </div>
</template>

<style scoped>
.settings-map {
  margin-bottom: 1rem;
}

.map-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin-bottom: 0.5rem;
}

.map-header h4 {
  margin: 0;
  font-size: 1rem;
  color: #12324f;
}

.map-header p {
  margin: 0.1rem 0 0;
  font-size: 0.9rem;
}

.add-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.add-form label {
  font-weight: 500;
}

.add-form input {
  flex: 1 1 12rem;
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid #9aa7b4;
  border-radius: 4px;
}

.add-actions {
  display: flex;
  gap: 0.5rem;
}

.map-body {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 28rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.map-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) 2fr auto;
  gap: 0.75rem;
  align-items: start;
  padding: 0.6rem 0.75rem;
}

.map-key {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.map-key label {
  font-size: 0.85rem;
  color: #5c6b7a;
}

.map-key input {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid #9aa7b4;
  border-radius: 4px;
}

.display-title {
  font-size: 0.85rem;
  color: #5c6b7a;
}

.map-value {
  min-width: 0;
}

.empty {
  margin: 0.25rem 0 0;
  font-size: 0.9rem;
}

@media (max-width: 640px) {
  .map-row {
    grid-template-columns: 1fr;
  }
}
</style>
