<script setup lang="ts">
/** One job's detail: poll + SSE progress, cancel, typed errors, results. */

import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  cancelJob,
  getJob,
  isActive,
  isPartialFailure,
  jobEventsStreamUrl,
  jobStatusLabel,
  progressOf,
  type Job,
} from '../api/jobs'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { formatTime } from '../utils/format'

type JobEvent = {
  id?: number
  ts?: string | null
  kind?: string | null
  data?: Record<string, unknown> | null
}

type LogRow = {
  ts: string
  level: string
  logger: string
  message: string
}

const LOG_TAIL_LIMIT = 300
const SCROLL_BOTTOM_THRESHOLD = 12

const route = useRoute()
const jobId = computed(() => String(route.params.id ?? ''))

const job = ref<Job | null>(null)
const events = ref<JobEvent[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const cancelError = ref<string | null>(null)
const cancelOpen = ref(false)
const cancelling = ref(false)
const logContainer = ref<HTMLElement | null>(null)
const logsScrolledToBottom = ref(true)

let pollTimer: ReturnType<typeof setInterval> | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let source: EventSource | null = null

const progress = computed(() => (job.value ? progressOf(job.value) : {}))
const partial = computed(() => (job.value ? isPartialFailure(job.value) : false))
const canCancel = computed(
  () =>
    job.value !== null &&
    ['queued', 'running'].includes(job.value.status) &&
    !job.value.cancel_requested,
)

const errorCode = computed(() => {
  const code = job.value?.error?.code
  return typeof code === 'string' ? code : null
})
const errorMessage = computed(() => {
  const message = job.value?.error?.message
  return typeof message === 'string' ? message : null
})
const errorRetryable = computed(() => job.value?.error?.retryable === true)

const logRows = computed<LogRow[]>(() => {
  const rows: LogRow[] = []
  for (const event of events.value) {
    if (event.kind !== 'log' || !event.data) continue
    const data = event.data
    rows.push({
      ts: String(data.ts ?? event.ts ?? ''),
      level: String(data.level ?? ''),
      logger: String(data.logger ?? ''),
      message: String(data.message ?? ''),
    })
  }
  return rows
})

/** Lifecycle events only — log rows live in the log pane, not here. */
const statusEvents = computed<JobEvent[]>(() =>
  events.value.filter((event) => event.kind !== 'log'),
)

const visibleLogs = computed<LogRow[]>(() => {
  if (logRows.value.length <= LOG_TAIL_LIMIT) return logRows.value
  return logRows.value.slice(logRows.value.length - LOG_TAIL_LIMIT)
})

const logTruncatedCount = computed(
  () => logRows.value.length - visibleLogs.value.length,
)

const jobFinished = computed(
  () =>
    job.value !== null &&
    ['succeeded', 'failed', 'cancelled'].includes(job.value.status),
)

function logLevelClass(level: string): string {
  switch (level) {
    case 'ERROR':
      return 'log-level-error'
    case 'WARNING':
      return 'log-level-warning'
    case 'INFO':
      return 'log-level-info'
    default:
      return 'log-level-debug'
  }
}

function formatLogTime(iso: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleTimeString(undefined, { hour12: false })
}

function updateLogsScrolledToBottom(): void {
  const el = logContainer.value
  if (!el) return
  logsScrolledToBottom.value =
    el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_BOTTOM_THRESHOLD
}

function scrollLogsToBottom(): void {
  const el = logContainer.value
  if (!el) return
  el.scrollTop = el.scrollHeight
  logsScrolledToBottom.value = true
}

watch(
  () => visibleLogs.value.length,
  async () => {
    await nextTick()
    if (logsScrolledToBottom.value) scrollLogsToBottom()
  },
)

/** Render the job result as entity/artifact links (§7.1.9). */
const resultLinks = computed<{ label: string; to: string }[]>(() => {
  const jobValue = job.value
  if (!jobValue?.result) return []
  const result = jobValue.result
  const ids = (key: string): (string | number)[] =>
    Array.isArray(result[key]) ? (result[key] as (string | number)[]) : []
  const links: { label: string; to: string }[] = []
  if (jobValue.job_type === 'match') {
    for (const id of ids('unmatched_family_ids')) {
      links.push({ label: `Unmatched family ${id}`, to: `/audit/families/${id}` })
    }
    for (const id of ids('unmatched_sponsor_ids')) {
      links.push({
        label: `Unmatched sponsor ${id}`,
        to: `/audit/sponsors/${id}`,
      })
    }
    if (result.run_id !== undefined && result.run_id !== null) {
      links.push({ label: `Match run ${result.run_id}`, to: '/match' })
    }
  } else if (jobValue.job_type === 'screen') {
    for (const id of ids('ai_failed')) {
      links.push({ label: `Unscreened child ${id}`, to: `/audit/children/${id}` })
    }
  } else if (jobValue.job_type === 'clarify_generate') {
    for (const id of ids('failed_family_ids')) {
      links.push({ label: `Draft failed for family ${id}`, to: `/clarify/${id}` })
    }
  } else if (jobValue.job_type === 'generate') {
    links.push({ label: 'Generation artifacts', to: '/generate' })
  } else if (jobValue.job_type === 'stats') {
    links.push({ label: 'Stats reports', to: '/stats' })
  } else if (jobValue.job_type === 'clarify_export') {
    links.push({ label: 'Clarification artifacts', to: '/clarify' })
  } else if (jobValue.job_type === 'acquire') {
    links.push({ label: 'Audit rows from the new snapshot', to: '/audit' })
  }
  return links
})

const resultJson = computed(() =>
  job.value?.result ? JSON.stringify(job.value.result, null, 2) : '',
)

function mergeEvents(next: JobEvent[]): void {
  const seen = new Set(events.value.map((event) => event.id))
  for (const event of next) {
    if (event.id !== undefined && seen.has(event.id)) continue
    events.value.push(event)
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const fresh = await getJob(jobId.value)
    job.value = fresh
    error.value = null
    mergeEvents((fresh.events ?? []) as JobEvent[])
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'job request failed'
  } finally {
    loading.value = false
  }
}

function connect(): void {
  if (typeof EventSource === 'undefined') return
  if (job.value && !isActive(job.value)) return
  source?.close()
  source = new EventSource(jobEventsStreamUrl(jobId.value))
  source.onmessage = (message: MessageEvent<string>) => {
    let parsed: JobEvent
    try {
      parsed = JSON.parse(message.data) as JobEvent
    } catch {
      return
    }
    mergeEvents([parsed])
    if (parsed.kind === 'progress' && parsed.data && job.value) {
      job.value = { ...job.value, progress: parsed.data }
    }
    if (['succeeded', 'failed', 'cancelled'].includes(parsed.kind ?? '')) {
      source?.close()
      void load()
    }
  }
  source.onerror = () => {
    source?.close()
    source = null
    if (retryTimer !== null) clearTimeout(retryTimer)
    if (job.value && isActive(job.value)) {
      retryTimer = setTimeout(connect, 3000)
    }
  }
}

async function requestCancel(): Promise<void> {
  cancelOpen.value = false
  cancelling.value = true
  cancelError.value = null
  try {
    job.value = await cancelJob(jobId.value)
    mergeEvents(((job.value.events ?? []) as JobEvent[]) ?? [])
  } catch (failure) {
    cancelError.value =
      failure instanceof ApiError
        ? failure.message
        : 'cancel request failed'
  } finally {
    cancelling.value = false
  }
}

onMounted(async () => {
  await load()
  connect()
  pollTimer = setInterval(() => {
    if (document.hidden) return
    if (job.value && isActive(job.value)) void load()
  }, 2000)
})

onUnmounted(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
  if (retryTimer !== null) clearTimeout(retryTimer)
  source?.close()
})
</script>

<template>
  <section>
    <p><RouterLink to="/jobs">← All jobs</RouterLink></p>

    <p v-if="loading && !job" role="status">Loading job…</p>
    <p v-else-if="error && !job" class="error-text" role="alert" data-testid="job-error">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>

    <template v-if="job">
      <div class="job-detail-layout">
        <div class="job-detail-main">
          <div class="status-line" style="justify-content: space-between">
            <h1 style="margin: 0">
              Job #{{ job.id }} · {{ job.job_type.replaceAll('_', ' ') }}
            </h1>
            <span class="badge" data-testid="job-status" :data-status="job.status">
              {{ jobStatusLabel(job.status) }}
            </span>
          </div>
          <p v-if="error" class="error-text" role="alert">{{ error }}</p>

          <div class="card" style="margin-top: 0.75rem">
            <dl class="status-line" style="gap: 1.5rem">
              <div><dt class="muted">Requester</dt><dd>{{ job.requester }}</dd></div>
              <div><dt class="muted">Attempt</dt><dd>{{ job.attempt }}</dd></div>
              <div><dt class="muted">Created</dt><dd>{{ formatTime(job.created_at) }}</dd></div>
              <div><dt class="muted">Started</dt><dd>{{ formatTime(job.started_at) }}</dd></div>
              <div><dt class="muted">Finished</dt><dd>{{ formatTime(job.finished_at) }}</dd></div>
            </dl>

            <h2>Progress</h2>
            <p data-testid="job-progress">
              {{ progress.phase ?? '—' }} · {{ progress.completed ?? 0 }}<template
                v-if="progress.total !== null && progress.total !== undefined"
              >/{{ progress.total }}</template>
              completed · {{ progress.failed ?? 0 }} failed
            </p>
            <progress
              v-if="progress.total"
              :value="progress.completed ?? 0"
              :max="progress.total"
            />

            <p
              v-if="partial"
              class="banner banner-error"
              role="alert"
              data-testid="partial-failure"
            >
              Partial failure: {{ progress.failed }} unit(s) failed while
              {{ progress.completed }} completed. Completed work is kept.
            </p>

            <p v-if="job.cancel_requested && job.status !== 'cancelled'" class="muted">
              Cancellation requested — the worker stops at the next safe point.
            </p>

            <p v-if="cancelError" class="error-text" role="alert">{{ cancelError }}</p>
            <button
              v-if="canCancel"
              type="button"
              class="danger"
              :disabled="cancelling"
              data-testid="cancel-job"
              @click="cancelOpen = true"
            >
              Request cancellation
            </button>

            <ConfirmDialog
              :open="cancelOpen"
              title="Cancel this job?"
              message="Cancellation is cooperative: completed items stay committed and the worker stops at the next safe point."
              confirm-label="Request cancellation"
              :dangerous="true"
              @confirm="requestCancel"
              @cancel="cancelOpen = false"
            />
          </div>

          <section
            v-if="job.error"
            class="card"
            style="margin-top: 1rem; border-color: #b3261e"
            role="alert"
            data-testid="job-error-detail"
          >
            <h2 style="margin-top: 0">Job failed</h2>
            <p>
              <strong data-testid="job-error-code">{{ errorCode ?? 'error' }}</strong>:
              {{ errorMessage ?? 'no error message' }}
            </p>
            <p v-if="errorCode === 'recovery_required'" class="notice">
              The worker lost this job's lease before it finished. The job is not
              retried automatically because partial writes may exist; review the
              affected data and rerun the command explicitly.
            </p>
            <p v-else-if="errorRetryable" class="notice">
              This failure is retryable — enqueue the command again.
            </p>
            <p v-else class="muted">
              This failure is not retryable; correct the input or season state first.
            </p>
          </section>

          <section class="card" style="margin-top: 1rem">
            <h2 style="margin-top: 0">Results</h2>
            <p v-if="!job.result" class="muted">No result recorded yet.</p>
            <template v-else>
              <ul v-if="resultLinks.length" data-testid="result-links">
                <li v-for="link in resultLinks" :key="link.to + link.label">
                  <RouterLink :to="link.to">{{ link.label }}</RouterLink>
                </li>
              </ul>
              <details>
                <summary>Raw result</summary>
                <pre>{{ resultJson }}</pre>
              </details>
            </template>
          </section>

          <section class="card" style="margin-top: 1rem">
            <h2 style="margin-top: 0">Events</h2>
            <p v-if="statusEvents.length === 0" class="muted">No events recorded yet.</p>
            <ol v-else data-testid="job-events">
              <li v-for="(event, index) in statusEvents" :key="event.id ?? index">
                <span class="badge">{{ event.kind ?? 'event' }}</span>
                <span class="muted"> {{ formatTime(event.ts) }}</span>
              </li>
            </ol>
          </section>
        </div>

        <section class="card job-log-pane" aria-label="Backend log">
          <header class="job-log-header">
            <div class="job-log-title">
              <h2 style="margin: 0">Backend log</h2>
              <span class="badge" :data-status="job.status">
                {{ jobStatusLabel(job.status) }}
              </span>
              <span v-if="logRows.length > 0" class="muted log-count">
                {{ logRows.length }} line{{ logRows.length === 1 ? '' : 's' }}
              </span>
            </div>
            <button
              v-if="!logsScrolledToBottom"
              type="button"
              class="job-log-jump"
              @click="scrollLogsToBottom"
            >
              Jump to latest ↓
            </button>
          </header>

          <div
            ref="logContainer"
            class="job-log-body"
            @scroll="updateLogsScrolledToBottom"
          >
            <p
              v-if="logRows.length === 0"
              class="muted job-log-empty"
              data-testid="job-log-empty"
            >
              <template v-if="jobFinished">Job finished with no log output.</template>
              <template v-else>No log output yet.</template>
            </p>
            <ol v-else class="job-log-lines" data-testid="job-log-lines">
              <li
                v-for="(row, index) in visibleLogs"
                :key="index"
                class="job-log-line"
                data-testid="job-log-line"
              >
                <span class="job-log-time">{{ formatLogTime(row.ts) }}</span>
                <span class="job-log-level" :class="logLevelClass(row.level)">
                  {{ row.level || '—' }}
                </span>
                <span class="job-log-logger muted">{{ row.logger || '—' }}</span>
                <span class="job-log-message">{{ row.message }}</span>
              </li>
            </ol>
            <p
              v-if="logTruncatedCount > 0"
              class="muted job-log-truncated"
              data-testid="job-log-truncated"
            >
              Showing last {{ visibleLogs.length }} of
              {{ logRows.length }} lines.
            </p>
          </div>
        </section>
      </div>
    </template>
  </section>
</template>

<style scoped>
.job-detail-layout {
  display: grid;
  gap: 1rem;
  grid-template-columns: minmax(0, 1.5fr) minmax(320px, 1fr);
  align-items: start;
}

.job-detail-main {
  min-width: 0;
}

.job-log-pane {
  display: flex;
  flex-direction: column;
  max-height: min(70vh, 600px);
  padding: 0;
  overflow: hidden;
}

.job-log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.9rem 1rem;
  border-bottom: 1px solid #d7dee6;
  flex-wrap: wrap;
}

.job-log-title {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.job-log-title h2 {
  font-size: 1.25rem;
}

.log-count {
  font-size: 0.85rem;
}

.job-log-jump {
  font-size: 0.85rem;
  padding: 0.2rem 0.5rem;
  white-space: nowrap;
}

.job-log-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 0.6rem 1rem;
  font-family: ui-monospace, 'Cascadia Mono', 'Menlo', monospace;
  font-size: 0.82rem;
  line-height: 1.45;
}

.job-log-empty {
  margin: 0;
  padding: 1rem 0;
  text-align: center;
}

.job-log-lines {
  margin: 0;
  padding: 0;
  list-style: none;
}

.job-log-line {
  display: grid;
  grid-template-columns: 5rem 4rem 7rem 1fr;
  gap: 0.6rem;
  padding: 0.15rem 0;
  border-bottom: 1px solid #f0f2f5;
}

.job-log-time {
  color: #5c6b7a;
}

.job-log-level {
  font-weight: 600;
  text-transform: uppercase;
}

.job-log-level.log-level-error {
  color: #8c1c16;
}

.job-log-level.log-level-warning {
  color: #9a7b12;
}

.job-log-level.log-level-info {
  color: #1c2530;
}

.job-log-level.log-level-debug {
  color: #8a99a8;
}

.job-log-logger {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-log-message {
  white-space: pre-wrap;
  word-break: break-word;
}

.job-log-truncated {
  margin: 0.5rem 0 0;
  padding: 0.4rem 0;
  text-align: center;
  border-top: 1px dashed #d7dee6;
  font-size: 0.78rem;
}

@media (max-width: 860px) {
  .job-detail-layout {
    grid-template-columns: 1fr;
  }

  .job-log-pane {
    max-height: 50vh;
  }
}
</style>
