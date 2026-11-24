<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  title: string
  src: string
}>()

const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDivElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
let opener: HTMLElement | null = null

watch(
  () => props.open,
  async (open, wasOpen) => {
    if (open) {
      opener = document.activeElement as HTMLElement | null
      await nextTick()
      closeButton.value?.focus()
    } else if (wasOpen) {
      opener?.focus()
      opener = null
    }
  },
  { immediate: true },
)

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return
  const focusable = dialog.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), iframe, [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
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
  <div v-if="open" class="dialog-backdrop" @click.self="emit('close')">
    <div
      ref="dialog"
      class="dialog popup-frame"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      :aria-label="title"
      @keydown="onKeydown"
    >
      <header class="popup-header">
        <h2>{{ title }}</h2>
        <button ref="closeButton" type="button" data-testid="popup-frame-close" @click="emit('close')">
          Close
        </button>
      </header>
      <iframe :src="src" :title="title" />
    </div>
  </div>
</template>

<style scoped>
.popup-frame {
  display: flex;
  flex-direction: column;
  width: min(76rem, 94vw);
  height: min(90vh, 64rem);
  max-width: none;
  min-height: 18rem;
  overflow: hidden;
}

.popup-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.popup-header h2 {
  margin: 0;
}

iframe {
  width: 100%;
  flex: 1;
  min-height: 0;
  border: 0;
}
</style>
