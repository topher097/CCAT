<script setup lang="ts">
/** /clarify: flagged families, contact state, and CSV export (§7.1.7). */

import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError } from '../api/client'
import {
  enqueueClarifyExport,
  listClarifications,
  type ClarifyFamily,
} from '../api/clarify'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { useJobPopup } from '../composables/useJobPopup'

const { openJob } = useJobPopup()
const session = useSessionStore()

const families = ref<ClarifyFamily[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const exportOpen = ref(false)
const exporting = ref(false)
const exportError = ref<string | null>(null)

const seasonName = computed(() => session.season?.name ?? '')

async function load(): Promise<void> {
  if (!seasonName.value) {
    error.value = 'No active season loaded.'
    return
  }
  loading.value = true
  error.value = null
  try {
    families.value = await listClarifications(seasonName.value)
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'clarification request failed'
  } finally {
    loading.value = false
  }
}

function contactOf(family: ClarifyFamily): string {
  const first = (family.children ?? [])[0] as
    | Record<string, unknown>
    | undefined
  const email = String(first?.family_email ?? '').trim()
  return email
}

async function runExport(): Promise<void> {
  exportOpen.value = false
  exportError.value = null
  exporting.value = true
  try {
    const job = await enqueueClarifyExport(seasonName.value)
    openJob(job.id)
  } catch (failure) {
    exportError.value =
      failure instanceof ApiError
        ? failure.message
        : 'export request failed'
  } finally {
    exporting.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1 style="margin: 0">Clarify</h1>
      <div class="status-line">
        <button type="button" :disabled="loading" @click="load">Refresh</button>
        <button
          type="button"
          class="primary"
          :disabled="exporting"
          data-testid="clarify-export"
          @click="exportOpen = true"
        >
          Export CSV
        </button>
      </div>
    </div>
    <p class="muted">
      Families with flagged children. Drafts are never sent automatically.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>
    <p v-if="exportError" class="error-text" role="alert">{{ exportError }}</p>

    <p v-if="loading" role="status">Loading clarification families…</p>
    <p
      v-else-if="error"
      class="error-text"
      role="alert"
      data-testid="clarify-error"
    >
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p
      v-else-if="families.length === 0"
      class="muted"
      data-testid="clarify-empty"
    >
      No family has a flagged child with clarification notes.
    </p>
    <table v-else class="data" data-testid="clarify-list">
      <thead>
        <tr>
          <th>Family</th>
          <th>Contact</th>
          <th>Flagged children</th>
          <th>Draft</th>
          <th>Contacted</th>
          <th>Replied</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="family in families" :key="family.family_id">
          <td>
            <RouterLink :to="`/clarify/${family.family_id}`">
              {{ family.family_id }}
            </RouterLink>
          </td>
          <td>
            <span v-if="contactOf(family)">{{ contactOf(family) }}</span>
            <span v-else class="muted">no contact on file</span>
          </td>
          <td>{{ (family.children ?? []).length }}</td>
          <td>
            <span v-if="family.draft">v{{ family.draft.version }}</span>
            <span v-else class="muted">none</span>
          </td>
          <td>
            <span class="badge">{{ family.contacted ? 'yes' : 'no' }}</span>
          </td>
          <td>
            <span class="badge">{{ family.replied ? 'yes' : 'no' }}</span>
          </td>
          <td class="muted">{{ formatTime(family.updated_at) }}</td>
        </tr>
      </tbody>
    </table>

    <ConfirmDialog
      :open="exportOpen"
      title="Export clarification CSV"
      message="Queues a durable export job. The CSV is written as a tracked artifact and downloaded from the job once it finishes."
      confirm-label="Enqueue export"
      @confirm="runExport"
      @cancel="exportOpen = false"
    />
  </section>
</template>
