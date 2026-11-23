<script setup lang="ts">
/**
 * Popup listing every angel assigned to one sponsor; each row opens the
 * child's audit editor. Presentational: the parent owns the data and fetch.
 */

import { nextTick, onMounted, ref, watch } from 'vue'

/** One assigned angel, as the popup shows it. */
export type SponsorAngel = {
  childId: string
  angelId: string | null
  name: string
  age: number | null
  needs: string
  wishes: string
  dropoff: string | null
}

const props = defineProps<{
  open: boolean
  sponsorName: string
  angels: SponsorAngel[]
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{
  close: []
  retry: []
  openChild: [child: { id: string; name: string }]
}>()

const dialog = ref<HTMLDivElement | null>(null)
const angelListScroll = ref<HTMLDivElement | null>(null)
const angelNameColumn = ref('4rem')
let opener: HTMLElement | null = null

async function measureAngelNameColumn(): Promise<void> {
  await nextTick()
  const wrapper = angelListScroll.value
  if (!wrapper) return

  const probe = document.createElement('span')
  probe.style.position = 'absolute'
  probe.style.visibility = 'hidden'
  probe.style.whiteSpace = 'nowrap'
  wrapper.append(probe)

  let widest = 0
  for (const angel of props.angels) {
    probe.textContent = angel.name
    widest = Math.max(widest, probe.getBoundingClientRect().width)
  }
  probe.remove()

  const rem =
    Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
  const width = Math.min(Math.max(widest + 1.4 * rem, 4 * rem), 16 * rem)
  angelNameColumn.value = `${width}px`
}

onMounted(() => void measureAngelNameColumn())

watch(
  () => props.angels,
  () => void measureAngelNameColumn(),
  { deep: true },
)

watch(
  () => props.open,
  async (open) => {
    if (open) {
      opener = document.activeElement as HTMLElement | null
      await nextTick()
      void measureAngelNameColumn()
      const firstAngel = dialog.value?.querySelector<HTMLElement>('.angel-row')
      const firstFocusable = dialog.value?.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      const initialFocus = firstAngel ?? firstFocusable
      initialFocus?.focus()
    } else {
      opener?.focus()
      opener = null
    }
  },
  { immediate: true },
)

/** Esc closes; Tab stays inside the dialog (focus trap). */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
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
</script>

<template>
  <div
    v-if="open"
    class="dialog-backdrop sponsor-angels-backdrop"
    data-testid="sponsor-angels-backdrop"
    @click.self="emit('close')"
  >
    <div
      ref="dialog"
      class="dialog sponsor-angels-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="sponsor-angels-heading"
      data-testid="sponsor-angels-dialog"
      @keydown="onKeydown"
    >
      <header class="modal-heading">
        <h2 id="sponsor-angels-heading" data-testid="sponsor-angels-heading">
          Angels assigned to {{ sponsorName }}
        </h2>
        <button
          type="button"
          aria-label="Close assigned angels"
          data-testid="sponsor-angels-close"
          @click="emit('close')"
        >
          Close
        </button>
      </header>

      <p v-if="loading" role="status" data-testid="sponsor-angels-loading">
        Loading assigned angels…
      </p>

      <p
        v-else-if="error"
        class="error-text"
        role="alert"
        data-testid="sponsor-angels-error"
      >
        {{ error }}
        <button
          type="button"
          data-testid="sponsor-angels-retry"
          @click="emit('retry')"
        >
          Retry
        </button>
      </p>

      <p
        v-else-if="angels.length === 0"
        class="muted empty"
        data-testid="sponsor-angels-empty"
      >
        No angels assigned.
      </p>

      <div
        v-else
        ref="angelListScroll"
        class="angel-list-scroll"
        :style="{ '--angel-name-col': angelNameColumn }"
      >
        <div class="angel-grid angel-grid-heading" aria-hidden="true">
          <span data-testid="sponsor-angels-header-id">Angel ID</span>
          <span data-testid="sponsor-angels-header-name">Name</span>
          <span data-testid="sponsor-angels-header-age">Age</span>
          <span data-testid="sponsor-angels-header-needs">Needs</span>
          <span data-testid="sponsor-angels-header-wishes">Wishes</span>
          <span data-testid="sponsor-angels-header-dropoff">Drop-off</span>
        </div>

        <ul class="angel-list" data-testid="sponsor-angels-list">
          <li v-for="angel in angels" :key="angel.childId">
            <button
              type="button"
              class="angel-grid angel-row"
              data-testid="sponsor-angel-row"
              :data-child-id="angel.childId"
              :aria-label="`Open child audit for ${angel.name}`"
              @click="emit('openChild', { id: angel.childId, name: angel.name })"
            >
              <span data-testid="sponsor-angel-id">{{ angel.angelId || '—' }}</span>
              <span data-testid="sponsor-angel-name">{{ angel.name }}</span>
              <span data-testid="sponsor-angel-age">{{ angel.age ?? '—' }}</span>
              <span data-testid="sponsor-angel-needs">{{ angel.needs || '—' }}</span>
              <span data-testid="sponsor-angel-wishes">{{ angel.wishes || '—' }}</span>
              <span data-testid="sponsor-angel-dropoff">{{ angel.dropoff || '—' }}</span>
            </button>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sponsor-angels-dialog {
  display: flex;
  flex-direction: column;
  width: min(74rem, 96vw);
  max-width: none;
  max-height: calc(100dvh - 2rem);
  overflow: hidden;
}

.modal-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.modal-heading h2 {
  margin-top: 0;
}

.angel-list-scroll {
  position: relative;
  min-height: 0;
  max-height: min(68vh, 44rem);
  overflow: auto;
  border: 1px solid #d7dee6;
  border-radius: 6px;
}

.angel-grid {
  display: grid;
  grid-template-columns:
    minmax(5rem, 0.65fr) var(--angel-name-col) 4rem
    minmax(10rem, 1.5fr) minmax(10rem, 1.5fr) minmax(8rem, 1fr);
  gap: 0.5rem 0.75rem;
  align-items: start;
  min-width: 52rem;
  padding: 0.55rem 0.7rem;
}

.angel-grid-heading {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #eef2f6;
  color: #1c2530;
  font-size: 0.85rem;
  font-weight: 600;
  text-align: left;
}

.angel-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.angel-row {
  width: 100%;
  border: 0;
  border-top: 1px solid #e2e8ee;
  border-radius: 0;
  background: #fff;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.angel-row:hover {
  background: #f1f7fd;
}

.angel-row:focus-visible {
  position: relative;
  outline: 2px solid #0b5cad;
  outline-offset: -2px;
}

.angel-row > span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.angel-row > span:first-child {
  font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
  font-weight: 700;
}
</style>
