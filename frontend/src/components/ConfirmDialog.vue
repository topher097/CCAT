<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  title: string
  message: string
  confirmLabel: string
  dangerous?: boolean
}>()

const emit = defineEmits<{ confirm: []; cancel: [] }>()
const confirmButton = ref<HTMLButtonElement | null>(null)

const confirmCss = computed(() => (props.dangerous ? 'danger' : 'primary'))

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    await nextTick()
    confirmButton.value?.focus()
  },
)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('cancel')
}
</script>

<template>
  <div v-if="open" class="dialog-backdrop" @click.self="emit('cancel')">
    <div
      class="dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      @keydown="onKeydown"
    >
      <h2 style="margin-top: 0">{{ title }}</h2>
      <p>{{ message }}</p>
      <div class="dialog-actions">
        <button type="button" @click="emit('cancel')">Cancel</button>
        <button
          ref="confirmButton"
          type="button"
          :class="confirmCss"
          data-testid="confirm"
          @click="emit('confirm')"
        >
          {{ confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
