<script setup lang="ts">
/** Modal asking for the required reason before a family is held (D14). */

import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  familyLabel: string
}>()

const emit = defineEmits<{ confirm: [note: string]; cancel: [] }>()

const dialog = ref<HTMLDivElement | null>(null)
const noteField = ref<HTMLTextAreaElement | null>(null)
const note = ref('')

const canConfirm = computed(() => note.value.trim().length > 0)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    note.value = ''
    await nextTick()
    noteField.value?.focus()
  },
)

/** Esc cancels; Tab stays inside the dialog (focus trap). */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('cancel')
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return
  const focusable = dialog.value.querySelectorAll<HTMLElement>(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
  )
  if (focusable.length === 0) return
  const first = focusable[0]!
  const last = focusable[focusable.length - 1]!
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function submit(): void {
  if (!canConfirm.value) return
  emit('confirm', note.value.trim())
}
</script>

<template>
  <div v-if="open" class="dialog-backdrop" @click.self="emit('cancel')">
    <div
      ref="dialog"
      class="dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="`Hold ${familyLabel}`"
      @keydown="onKeydown"
    >
      <h2 style="margin-top: 0">Hold {{ familyLabel }}</h2>
      <p>
        A hold keeps this family out of the automatic matcher without placing
        it. The reason is required.
      </p>
      <label for="hold-note">Reason</label>
      <textarea
        id="hold-note"
        ref="noteField"
        v-model="note"
        data-testid="hold-note"
        rows="3"
        style="width: 100%"
        placeholder="Why is this family set aside?"
      />
      <div class="dialog-actions">
        <button type="button" @click="emit('cancel')">Cancel</button>
        <button
          type="button"
          class="primary"
          data-testid="hold-confirm"
          :disabled="!canConfirm"
          @click="submit"
        >
          Hold family
        </button>
      </div>
    </div>
  </div>
</template>
