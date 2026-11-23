<script setup lang="ts">
/** The audit editor in a popup, for pages that are not the audit views. */

import { nextTick, ref, watch } from 'vue'

import type { Entity } from '../api/audit'
import { formDirty } from '../composables/useDirtyGuard'
import EntityEditor from './EntityEditor.vue'

const props = defineProps<{
  open: boolean
  entity: Entity
  entityKey: string
  /** Accessible name, e.g. the contact or child name. */
  title?: string
}>()

const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDivElement | null>(null)
let opener: HTMLElement | null = null

watch(
  () => props.open,
  async (open) => {
    if (open) {
      opener = document.activeElement as HTMLElement | null
      await nextTick()
      dialog.value?.focus()
    } else {
      opener?.focus()
      opener = null
    }
  },
  { immediate: true },
)

/** Closing drops unsaved edits, so ask first (same prompt as the router guard). */
function requestClose(): void {
  if (formDirty.value && !window.confirm('Discard unsaved changes?')) return
  emit('close')
}

/** Esc closes; Tab stays inside the popup. */
function onKeydown(event: KeyboardEvent): void {
  // Keys inside a nested confirm dialog belong to that dialog.
  if ((event.target as HTMLElement).closest('.dialog') !== dialog.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    requestClose()
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return
  const focusable = dialog.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )
  if (focusable.length === 0) return
  const first = focusable[0]!
  const last = focusable[focusable.length - 1]!
  const active = document.activeElement
  if (event.shiftKey && (active === first || active === dialog.value)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <div v-if="open" class="dialog-backdrop" @click.self="requestClose">
    <div
      ref="dialog"
      class="dialog entity-modal"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      :aria-label="title || `${entity} ${entityKey}`"
      data-testid="entity-modal"
      @keydown="onKeydown"
    >
      <EntityEditor :entity="entity" :entity-key="entityKey" embedded>
        <template #back>
          <button type="button" data-testid="entity-modal-close" @click="requestClose">
            ✕ Close
          </button>
        </template>
      </EntityEditor>
    </div>
  </div>
</template>

<style scoped>
/* Wider than the 34rem confirm dialog: the editor is a three-column table. */
.entity-modal {
  width: min(56rem, 92vw);
  max-width: none;
  max-height: 90vh;
  overflow-y: auto;
  outline: none;
}
</style>
