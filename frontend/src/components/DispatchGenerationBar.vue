<script setup lang="ts">
/** Generation shortcuts for the dispatch work list. */

import { ref } from 'vue'
import { RouterLink } from 'vue-router'

import type { DispatchTotals } from '../api/dispatch'
import ConfirmDialog from './ConfirmDialog.vue'

const props = withDefaults(
  defineProps<{
    totals: DispatchTotals
    enqueueing?: boolean
    error?: string | null
    conflictJobId?: number | null
  }>(),
  { enqueueing: false, error: null, conflictJobId: null },
)
const emit = defineEmits<{ run: [options: Record<string, unknown>] }>()

type PendingRun = {
  title: string
  message: string
  confirmLabel: string
  options: Record<string, unknown>
  dangerous: boolean
}

const overwrite = ref(false)
const sponsorId = ref('')
const childId = ref('')
const pending = ref<PendingRun | null>(null)
const localError = ref<string | null>(null)

function request(
  title: string,
  options: Record<string, unknown>,
  dangerous = false,
): void {
  localError.value = null
  pending.value = {
    title,
    message: `${title} is queued as a durable generate job.${
      dangerous ? ' Existing completed files will be overwritten.' : ''
    }`,
    confirmLabel: title,
    options,
    dangerous,
  }
}

function requestSheets(regenerate = false): void {
  request(regenerate ? 'Regenerate all sheets' : 'Generate pending sheets', {
    only: 'sheets',
    overwrite: regenerate,
  }, regenerate)
}

function requestPackets(regenerate = false): void {
  request(regenerate ? 'Regenerate all packets' : 'Generate eligible packets', {
    only: 'packets',
    overwrite: regenerate,
  }, regenerate)
}

function requestBoth(): void {
  request('Generate sheets & packets', {
    only: 'both',
    overwrite: overwrite.value,
  }, overwrite.value)
}

/** Shared by the generation card and each sponsor row. */
function requestSponsor(id: number, regenerate = false): void {
  request(regenerate ? 'Regenerate sponsor packet' : 'Generate sponsor packet', {
    sponsor: id,
    only: 'packets',
    overwrite: regenerate,
  }, regenerate)
}

function requestSponsorInput(): void {
  const id = Number(sponsorId.value)
  if (!Number.isInteger(id) || id < 1) {
    localError.value = 'Enter a valid sponsor ID.'
    return
  }
  requestSponsor(id, overwrite.value)
}

function requestChild(): void {
  const id = childId.value.trim()
  if (!id) {
    localError.value = 'Enter a child record ID.'
    return
  }
  request('Generate child sheet', {
    child: id,
    only: 'sheets',
    overwrite: overwrite.value,
  }, overwrite.value)
}

function enqueue(): void {
  const run = pending.value
  pending.value = null
  if (run) emit('run', run.options)
}

defineExpose({ requestSponsor })
</script>

<template>
  <section class="card" data-testid="generation-bar">
    <h2 style="margin-top: 0">Generation</h2>
    <label style="display: block; margin-bottom: 0.65rem">
      <input v-model="overwrite" type="checkbox" name="overwrite" />
      Overwrite completed files
    </label>

    <div class="status-line">
      <button
        type="button"
        data-testid="generate-sheets"
        :disabled="props.enqueueing"
        @click="requestSheets()"
      >
        Generate pending sheets ({{ props.totals.sheets_missing }})
      </button>
      <button
        type="button"
        data-testid="generate-packets"
        :disabled="props.enqueueing"
        @click="requestPackets()"
      >
        Generate eligible packets ({{ props.totals.packets_missing }})
      </button>
      <button
        type="button"
        class="primary"
        data-testid="generate-both"
        :disabled="props.enqueueing"
        @click="requestBoth"
      >
        Generate sheets &amp; packets
      </button>
      <button
        v-if="overwrite"
        type="button"
        class="danger"
        data-testid="regenerate-sheets"
        :disabled="props.enqueueing"
        @click="requestSheets(true)"
      >
        Regenerate all sheets ({{ props.totals.sheets_ready + props.totals.sheets_missing }})
      </button>
      <button
        v-if="overwrite"
        type="button"
        class="danger"
        data-testid="regenerate-packets"
        :disabled="props.enqueueing"
        @click="requestPackets(true)"
      >
        Regenerate all packets ({{ props.totals.packets_ready + props.totals.packets_missing }})
      </button>
    </div>

    <details style="margin-top: 0.8rem">
      <summary>Generate one sponsor or child</summary>
      <div class="filters" style="margin-top: 0.65rem">
        <label>
          Sponsor ID
          <input v-model="sponsorId" type="number" min="1" name="sponsor_id" />
        </label>
        <button
          type="button"
          data-testid="generate-sponsor"
          :disabled="props.enqueueing"
          @click="requestSponsorInput"
        >
          Generate sponsor packet
        </button>
      </div>
      <div class="filters">
        <label>
          Child record ID
          <input v-model="childId" type="text" name="child_id" />
        </label>
        <button
          type="button"
          data-testid="generate-child"
          :disabled="props.enqueueing"
          @click="requestChild"
        >
          Generate child sheet
        </button>
      </div>
    </details>

    <p v-if="localError || props.error" class="error-text" role="alert" data-testid="generation-error">
      {{ localError || props.error }}
      <RouterLink
        v-if="props.conflictJobId !== null"
        :to="`/jobs/${props.conflictJobId}`"
        data-testid="generation-conflict-link"
      >
        Open active job #{{ props.conflictJobId }}
      </RouterLink>
    </p>

    <ConfirmDialog
      :open="pending !== null"
      :title="pending?.title ?? 'Generate files'"
      :message="pending?.message ?? ''"
      :confirm-label="pending?.confirmLabel ?? 'Generate'"
      :dangerous="pending?.dangerous ?? false"
      @confirm="enqueue"
      @cancel="pending = null"
    />
  </section>
</template>
