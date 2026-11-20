<script setup lang="ts">
/** Cross-command job center list (§7.1.9): filters + client pagination. */

import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  JOB_STATUSES,
  JOB_TYPES,
  isActive,
  jobStatusLabel,
  listJobs,
  progressOf,
  type Job,
} from '../api/jobs'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'

const session = useSessionStore()

const jobs = ref<Job[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const status = ref('all')
const type = ref('all')
const page = ref(1)
const pageSize = 25

const seasonName = computed(() => session.season?.name ?? '')

const filtered = computed(() =>
  jobs.value.filter(
    (job) =>
      (status.value === 'all' || job.status === status.value) &&
      (type.value === 'all' || job.job_type === type.value),
  ),
)

const totalPages = computed(() =>
  Math.max(1, Math.ceil(filtered.value.length / pageSize)),
)

const visible = computed(() => {
  const start = (page.value - 1) * pageSize
  return filtered.value.slice(start, start + pageSize)
})

async function load(): Promise<void> {
  if (!seasonName.value) {
    error.value = 'No active season loaded.'
    return
  }
  loading.value = true
  error.value = null
  try {
    jobs.value = await listJobs(seasonName.value)
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'job request failed'
  } finally {
    loading.value = false
  }
}

function setStatus(value: string): void {
  status.value = value
  page.value = 1
}

function setType(value: string): void {
  type.value = value
  page.value = 1
}

function go(next: number): void {
  page.value = Math.min(Math.max(1, next), totalPages.value)
}

function summary(job: Job): string {
  const progress = progressOf(job)
  const parts = [progress.phase, `${progress.completed ?? 0}`]
  if (progress.total !== null && progress.total !== undefined) {
    parts[1] += `/${progress.total}`
  }
  if (Number(progress.failed ?? 0) > 0) parts.push(`${progress.failed} failed`)
  return parts.filter(Boolean).join(' · ')
}

onMounted(() => void load())
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1 style="margin: 0">Jobs</h1>
      <button type="button" :disabled="loading" @click="load">Refresh</button>
    </div>
    <p class="muted">
      Every command runs as a durable, season-scoped job. One pipeline-class
      job may run at a time.
    </p>

    <div class="filters">
      <label>
        Status
        <select
          :value="status"
          aria-label="Filter by job status"
          @change="setStatus(($event.target as HTMLSelectElement).value)"
        >
          <option value="all">all</option>
          <option v-for="option in JOB_STATUSES" :key="option" :value="option">
            {{ jobStatusLabel(option) }}
          </option>
        </select>
      </label>
      <label>
        Type
        <select
          :value="type"
          aria-label="Filter by job type"
          @change="setType(($event.target as HTMLSelectElement).value)"
        >
          <option value="all">all</option>
          <option v-for="option in JOB_TYPES" :key="option" :value="option">
            {{ option.replaceAll('_', ' ') }}
          </option>
        </select>
      </label>
      <span class="muted" data-testid="jobs-total">
        {{ filtered.length }} jobs
      </span>
    </div>

    <p v-if="loading" role="status">Loading jobs…</p>
    <p v-else-if="error" class="error-text" role="alert" data-testid="jobs-error">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p v-else-if="filtered.length === 0" class="muted" data-testid="jobs-empty">
      No jobs match this filter.
    </p>

    <table v-else class="data">
      <thead>
        <tr>
          <th>Job</th>
          <th>Type</th>
          <th>Status</th>
          <th>Progress</th>
          <th>Requester</th>
          <th>Created</th>
          <th>Finished</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="job in visible" :key="job.id">
          <td>
            <RouterLink :to="`/jobs/${job.id}`">#{{ job.id }}</RouterLink>
          </td>
          <td>{{ job.job_type.replaceAll('_', ' ') }}</td>
          <td>
            <span class="badge" :data-status="job.status">
              {{ jobStatusLabel(job.status) }}
            </span>
            <span v-if="isActive(job)" class="muted"> · active</span>
          </td>
          <td>{{ summary(job) }}</td>
          <td>{{ job.requester }}</td>
          <td class="muted">{{ formatTime(job.created_at) }}</td>
          <td class="muted">{{ formatTime(job.finished_at) }}</td>
        </tr>
      </tbody>
    </table>

    <div class="pagination">
      <button type="button" :disabled="page <= 1" @click="go(page - 1)">
        Previous
      </button>
      <span data-testid="jobs-page">
        Page {{ page }} of {{ totalPages }}
      </span>
      <button
        type="button"
        :disabled="page >= totalPages"
        @click="go(page + 1)"
      >
        Next
      </button>
    </div>
  </section>
</template>
