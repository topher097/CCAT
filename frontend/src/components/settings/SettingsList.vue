<script setup lang="ts">
/** Array editor: rows of items with add / delete. */

import { computed } from 'vue'

import { useSettingsStore } from '../../stores/settings'
import { getAt, humanize, metaFor, templateFor, type Path } from '../../settings/tree'
import SettingsNode from './SettingsNode.vue'

const props = defineProps<{
  path: Path
}>()

const store = useSettingsStore()

const items = computed<unknown[]>(() => {
  const v = store.draft ? getAt(store.draft, props.path) : undefined
  return Array.isArray(v) ? v : []
})

const meta = computed(() => metaFor(props.path))
const label = computed(
  () => meta.value.label ?? humanize(props.path[props.path.length - 1] ?? ''),
)
const description = computed(() => meta.value.description)
const dotted = computed(() => props.path.join('.'))

const allScalar = computed(() =>
  items.value.every((item) => item === null || typeof item !== 'object'),
)

function itemPath(index: number): Path {
  return [...props.path, String(index)]
}

function itemName(index: number): string {
  return dotted.value + '.' + index
}

function add(): void {
  const template = templateFor(props.path, items.value)
  store.appendItem(props.path, template)
}

function remove(index: number): void {
  store.removeNode(itemPath(index))
}

function updateScalar(index: number, event: Event): void {
  const input = event.target as HTMLInputElement
  const item = items.value[index]
  if (typeof item === 'number') {
    // An emptied box keeps the last number: dropping the value would
    // change the item's type under the user.
    if (input.value === '') return
    const parsed = Number(input.value)
    if (Number.isNaN(parsed)) return
    store.setValue(itemPath(index), parsed)
  } else if (typeof item === 'boolean') {
    store.setValue(itemPath(index), input.checked)
  } else {
    store.setValue(itemPath(index), input.value)
  }
}

function scalarInputType(item: unknown): string {
  if (typeof item === 'number') return 'number'
  if (typeof item === 'boolean') return 'checkbox'
  return 'text'
}

function scalarValue(item: unknown): string | number {
  if (item === null) return ''
  if (typeof item === 'number') return item
  if (typeof item === 'boolean') return item ? 1 : 0
  return String(item)
}

function scalarChecked(item: unknown): boolean {
  return typeof item === 'boolean' ? item : false
}

function deleteLabel(index: number): string {
  const item = items.value[index]
  if (item === null || item === undefined) return `Delete item ${index + 1}`
  if (typeof item === 'object') return `Delete item ${index + 1}`
  return `Delete ${String(item)}`
}
</script>

<template>
  <div
    class="settings-list"
    :data-path="dotted"
    data-testid="settings-list"
  >
    <div class="list-header">
      <div>
        <h4>{{ label }}</h4>
        <p v-if="description" class="muted">{{ description }}</p>
      </div>
      <button
        type="button"
        class="primary"
        data-testid="settings-add-item"
        :data-path="dotted"
        @click="add"
      >
        Add
      </button>
    </div>

    <ul v-if="items.length > 0" :class="['list-body', { scalar: allScalar }]">
      <li
        v-for="(item, index) in items"
        :key="`${dotted}-${index}`"
        class="list-row"
      >
        <template v-if="allScalar">
          <input
            :name="itemName(index)"
            :type="scalarInputType(item)"
            :value="scalarValue(item)"
            :checked="scalarChecked(item)"
            :aria-label="`Value ${index + 1}`"
            @input="updateScalar(index, $event)"
          />
        </template>
        <SettingsNode v-else :path="itemPath(index)" />

        <button
          type="button"
          class="danger"
          data-testid="settings-delete"
          :data-path="itemPath(index).join('.')"
          data-kind="item"
          @click="remove(index)"
        >
          {{ deleteLabel(index) }}
        </button>
      </li>
    </ul>

    <p v-else class="muted empty">No entries yet.</p>
  </div>
</template>

<style scoped>
.settings-list {
  margin-bottom: 1rem;
}

.list-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin-bottom: 0.5rem;
}

.list-header h4 {
  margin: 0;
  font-size: 1rem;
  color: #12324f;
}

.list-header p {
  margin: 0.1rem 0 0;
  font-size: 0.9rem;
}

.list-body {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 24rem;
  overflow-y: auto;
  border: 1px solid #e2e8ee;
  border-radius: 4px;
}

.list-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.45rem 0.6rem;
  border-bottom: 1px solid #f0f2f5;
}

.list-row:last-child {
  border-bottom: none;
}

.list-body.scalar .list-row {
  display: grid;
  grid-template-columns: 1fr auto;
}

.list-body.scalar input[type='text'],
.list-body.scalar input[type='number'] {
  font: inherit;
  padding: 0.35rem 0.5rem;
  border: 1px solid #9aa7b4;
  border-radius: 4px;
  min-width: 0;
}

.list-body.scalar input[type='checkbox'] {
  justify-self: start;
}

.empty {
  margin: 0.25rem 0 0;
  font-size: 0.9rem;
}
</style>
