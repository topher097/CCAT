<script setup lang="ts">
import { ref, watch } from 'vue'

import {
  SPECIAL_NEEDS_OPTIONS,
  parseSpecialNeeds,
  serializeSpecialNeeds,
  type SpecialNeedsSelection,
} from '../utils/specialNeeds'

const props = defineProps<{ modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()

const selection = ref<SpecialNeedsSelection>(
  parseSpecialNeeds(props.modelValue),
)

// Legacy semantics: parsing "other: x" does not re-select the "other"
// piece; the editor mirrors the Python service exactly.
watch(
  () => props.modelValue,
  (value) => {
    if ((value ?? null) !== serializeSpecialNeeds(selection.value)) {
      selection.value = parseSpecialNeeds(value)
    }
  },
)

function publish(): void {
  emit('update:modelValue', serializeSpecialNeeds(selection.value))
}

function toggle(option: string): void {
  const fixed = new Set(selection.value.fixed)
  if (fixed.has(option)) fixed.delete(option)
  else fixed.add(option)
  selection.value = { ...selection.value, fixed }
  publish()
}

function setOther(text: string): void {
  selection.value = { ...selection.value, other: text }
  publish()
}
</script>

<template>
  <fieldset class="special-needs">
    <legend>Special needs</legend>
    <label
      v-for="option in SPECIAL_NEEDS_OPTIONS"
      :key="option"
      style="display: inline-flex; gap: 0.25rem; margin-right: 0.75rem"
    >
      <input
        type="checkbox"
        :checked="selection.fixed.has(option)"
        :name="`special-needs-${option}`"
        @change="toggle(option)"
      />
      {{ option }}
    </label>
    <label style="display: block; margin-top: 0.35rem">
      Other detail
      <input
        type="text"
        name="special-needs-other-text"
        :value="selection.other"
        @input="setOther(($event.target as HTMLInputElement).value)"
      />
    </label>
    <p class="muted" style="margin: 0.25rem 0 0">
      Stored as the legacy semicolon list; "other" is only kept while its
      checkbox is selected.
    </p>
  </fieldset>
</template>
