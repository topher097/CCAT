<script setup lang="ts">
/** One scalar setting: label, description, typed control, and field error. */

import { computed, ref, watch } from 'vue'

import { useSettingsStore } from '../../stores/settings'
import { getAt, humanize, metaFor, type Path } from '../../settings/tree'

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

// The control kind is latched: clearing a number must not swap the
// number box for a text box while the user is still in the field.
const control = ref<'boolean' | 'number' | 'text'>('text')
watch(
  value,
  (current) => {
    if (typeof current === 'boolean') control.value = 'boolean'
    else if (typeof current === 'number') control.value = 'number'
    else if (current !== null) control.value = 'text'
  },
  { immediate: true },
)

// While the user types, the raw text is the source of truth: a number
// like "1.5" passes through "1." on the way, and re-rendering from
// the parsed value would rewrite the field mid-keystroke.
const numberText = ref<string | null>(null)
let lastNumber: number | null = null

watch(value, (current) => {
  if (current !== lastNumber) numberText.value = null
})

const isLongString = computed(() => {
  const current = value.value
  return typeof current === 'string' && (current.length > 120 || current.includes('\n'))
})

function updateString(event: Event): void {
  const input = event.target as HTMLInputElement | HTMLTextAreaElement
  store.setValue(props.path, input.value)
}

function updateNumber(event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  numberText.value = raw
  if (raw.trim() === '') return
  const parsed = Number(raw)
  if (Number.isNaN(parsed)) return
  lastNumber = parsed
  store.setValue(props.path, parsed)
}

function onNumberBlur(): void {
  if (numberText.value === '') store.setValue(props.path, null)
  numberText.value = null
}

function updateBoolean(event: Event): void {
  const input = event.target as HTMLInputElement
  store.setValue(props.path, input.checked)
}
</script>

<template>
  <div class="settings-field" :data-path="dotted" data-testid="settings-field">
    <div class="field-main">
      <label :for="dotted">{{ label }}</label>
      <input
        v-if="control === 'boolean'"
        :id="dotted"
        :name="dotted"
        type="checkbox"
        :checked="value === true"
        @change="updateBoolean"
      />
      <input
        v-else-if="control === 'number'"
        :id="dotted"
        :name="dotted"
        type="number"
        :value="numberText ?? (typeof value === 'number' ? value : '')"
        @input="updateNumber"
        @blur="onNumberBlur"
      />
      <textarea
        v-else-if="isLongString"
        :id="dotted"
        :name="dotted"
        :value="(value as string | null) ?? ''"
        :placeholder="value === null ? 'null' : undefined"
        rows="6"
        @input="updateString"
      />
      <input
        v-else
        :id="dotted"
        :name="dotted"
        type="text"
        :value="(value as string | null) ?? ''"
        :placeholder="value === null ? 'null' : undefined"
        @input="updateString"
      />
    </div>
    <p v-if="description" class="muted description">{{ description }}</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.settings-field {
  padding: 0.5rem 0;
}

.field-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.75rem;
}

label {
  flex: 1 1 140px;
  font-weight: 500;
  min-width: 140px;
}

input[type='text'],
input[type='number'],
textarea {
  flex: 2 1 220px;
  min-width: 220px;
  font: inherit;
  padding: 0.4rem 0.55rem;
  border: 1px solid #9aa7b4;
  border-radius: 4px;
  background: #fff;
  color: inherit;
}

textarea {
  min-height: 6rem;
  max-height: 20rem;
  resize: vertical;
  line-height: 1.4;
}

input[type='checkbox'] {
  width: 1.15rem;
  height: 1.15rem;
  flex: 0 0 auto;
}

.description {
  margin: 0.15rem 0 0;
  font-size: 0.9rem;
}

.error-text {
  margin: 0.25rem 0 0;
  font-size: 0.9rem;
}
</style>
