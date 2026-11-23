<script setup lang="ts">
/** /dispatch: generate packets, then review and send sponsor email. */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import {
  draftDispatchEmails,
  getDispatchDraft,
  listDispatch,
  listDispatchChildren,
  patchDispatchDraft,
  sendDispatchEmail,
  type DispatchChild,
  type DispatchDraft,
  type DispatchDraftRequest,
  type DispatchListView,
  type DispatchRow,
} from '../api/dispatch'
import { ApiError } from '../api/client'
import {
  enqueueJob,
  isActive,
  listJobs,
  progressOf,
  type Job,
} from '../api/jobs'
import DispatchGenerationBar from '../components/DispatchGenerationBar.vue'
import DispatchSponsorTable from '../components/DispatchSponsorTable.vue'
import SponsorEmailModal from '../components/SponsorEmailModal.vue'
import { useSessionStore } from '../stores/session'
import { personName } from '../utils/sponsor'

const UNKNOWN_SEND_MESSAGE =
  'We do not know whether this email was accepted. Check the sent mail before sending again.'

const session = useSessionStore()
const router = useRouter()
const seasonName = computed(() => session.season?.name ?? '')
const data = ref<DispatchListView | null>(null)
const children = ref<DispatchChild[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const activeJob = ref<Job | null>(null)
const generationBar = ref<InstanceType<typeof DispatchGenerationBar> | null>(null)
const enqueueing = ref(false)
const generationError = ref<string | null>(null)
const conflictJobId = ref<number | null>(null)

const emailOpen = ref(false)
const selectedRow = ref<DispatchRow | null>(null)
const draft = ref<DispatchDraft | null>(null)
const draftLoading = ref(false)
const draftError = ref<string | null>(null)
const saving = ref(false)
const sending = ref(false)
const sendState = ref<'failed' | 'unknown' | null>(null)
const sendError = ref<string | null>(null)
let draftRequest = 0
let pollTimer: number | undefined

const canSend = computed(
  () => Boolean(data.value?.can_send && session.hasRole('admin')),
)
const jobProgress = computed(() =>
  activeJob.value ? progressOf(activeJob.value) : null,
)

function failureMessage(failure: unknown, fallback: string): string {
  return failure instanceof Error ? failure.message : fallback
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    data.value = null
    return
  }
  loading.value = true
  error.value = null
  try {
    data.value = await listDispatch(seasonName.value)
  } catch (failure) {
    error.value = failureMessage(failure, 'Dispatch request failed.')
  } finally {
    loading.value = false
  }
}

async function loadChildren(): Promise<void> {
  if (!seasonName.value) return
  try {
    children.value = (await listDispatchChildren(seasonName.value)).rows
  } catch {
    children.value = []
  }
}

async function pollJobs(): Promise<void> {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  pollTimer = undefined
  if (!seasonName.value) {
    activeJob.value = null
    return
  }
  try {
    const jobs = await listJobs(seasonName.value)
    activeJob.value =
      jobs.find((job) => job.job_type === 'generate' && isActive(job)) ?? null
  } catch {
    activeJob.value = null
  }
  if (activeJob.value) {
    pollTimer = window.setTimeout(() => void pollJobs(), 3000)
  }
}

function refresh(): void {
  void load()
  void loadChildren()
  void pollJobs()
}

function fieldErrors(context: Record<string, unknown>): string {
  const errors = context.errors
  if (!Array.isArray(errors)) return ''
  return errors
    .map((entry) => {
      const item = entry as { loc?: unknown; msg?: unknown }
      const loc = Array.isArray(item.loc) ? item.loc.join('.') : ''
      return [loc, String(item.msg ?? '')].filter(Boolean).join(': ')
    })
    .join('; ')
}

async function enqueueGenerate(options: Record<string, unknown>): Promise<void> {
  const season = seasonName.value
  if (!season) {
    generationError.value = 'No active season loaded.'
    return
  }
  enqueueing.value = true
  generationError.value = null
  conflictJobId.value = null
  try {
    const job = await enqueueJob(season, 'generate', options)
    activeJob.value = job
    await router.push(`/jobs/${job.id}`)
  } catch (failure) {
    if (!(failure instanceof ApiError)) {
      generationError.value = 'Enqueue failed.'
      return
    }
    const details = fieldErrors(failure.context)
    generationError.value = details
      ? `${failure.message}: ${details}`
      : failure.message
    if (failure.status === 409) {
      const match = /#(\d+)/.exec(failure.message)
      conflictJobId.value = match ? Number(match[1]) : null
    }
  } finally {
    enqueueing.value = false
  }
}

function generatePacket(sponsorId: number, overwrite: boolean): void {
  generationBar.value?.requestSponsor(sponsorId, overwrite)
}

function patchRow(sponsorId: number, patch: Partial<DispatchRow>): void {
  if (data.value) {
    data.value = {
      ...data.value,
      rows: data.value.rows.map((row) =>
        row.sponsor_id === sponsorId ? { ...row, ...patch } : row,
      ),
    }
  }
  if (selectedRow.value?.sponsor_id === sponsorId) {
    selectedRow.value = { ...selectedRow.value, ...patch }
  }
}

async function openEmail(row: DispatchRow): Promise<void> {
  const season = seasonName.value
  if (!season) return
  const request = ++draftRequest
  selectedRow.value = row
  draft.value = null
  draftError.value = null
  sendState.value = null
  sendError.value = null
  notice.value = null
  emailOpen.value = true
  draftLoading.value = true
  try {
    if (row.email_status === 'not_drafted') {
      const refreshed = await draftDispatchEmails(season, {
        sponsor_ids: [row.sponsor_id],
        all: false,
        overwrite: false,
      })
      if (request !== draftRequest) return
      data.value = refreshed
    }
    const loaded = await getDispatchDraft(season, row.sponsor_id)
    if (request === draftRequest) draft.value = loaded
  } catch (failure) {
    if (request === draftRequest) {
      draftError.value = failureMessage(failure, 'Draft request failed.')
    }
  } finally {
    if (request === draftRequest) draftLoading.value = false
  }
}

function closeEmail(): void {
  if (sending.value) return
  ++draftRequest
  emailOpen.value = false
  selectedRow.value = null
  draft.value = null
  draftLoading.value = false
  draftError.value = null
  sendState.value = null
  sendError.value = null
}

async function reloadDraft(): Promise<void> {
  if (selectedRow.value) await openEmail(selectedRow.value)
}

async function saveDraft(edit: DispatchDraftRequest): Promise<void> {
  const season = seasonName.value
  const current = draft.value
  const row = selectedRow.value
  if (!season || !current || !row || sending.value) return
  saving.value = true
  draftError.value = null
  try {
    const updated = await patchDispatchDraft(
      season,
      row.sponsor_id,
      edit,
      current.version,
    )
    draft.value = updated
    patchRow(row.sponsor_id, {
      draft_version: updated.version,
      draft_subject: updated.subject,
      email_status: 'draft_edited',
      email_error: null,
    })
  } catch (failure) {
    draftError.value = failureMessage(failure, 'Could not save the draft.')
  } finally {
    saving.value = false
  }
}

async function sendEmail(edit: DispatchDraftRequest): Promise<void> {
  const season = seasonName.value
  const current = draft.value
  const row = selectedRow.value
  if (!season || !current || !row || sending.value) return
  sending.value = true
  sendState.value = null
  sendError.value = null
  draftError.value = null
  try {
    const result = await sendDispatchEmail(season, row.sponsor_id, {
      version: current.version,
      subject: edit.subject,
      note: edit.note,
    })
    const editSubject = edit.subject ?? current.subject
    const editNote = edit.note ?? current.note
    if (result.state === 'sent') {
      const recipient = current.to_email || row.email || 'no address'
      notice.value = `Sent packet email to ${personName(row)} (${recipient}). Sponsor #${row.sponsor_id} is now marked dispatched.`
      patchRow(row.sponsor_id, {
        email_status: 'sent',
        email_sent_at: result.sent_at,
        email_error: null,
        draft_version: result.draft_version,
        packets_dispatched: true,
        packets_dispatched_at: result.sent_at,
      })
      sending.value = false
      closeEmail()
      return
    }

    sendState.value = result.state === 'unknown' ? 'unknown' : 'failed'
    sendError.value =
      sendState.value === 'unknown'
        ? UNKNOWN_SEND_MESSAGE
        : result.error || 'The packet email was refused.'
    draft.value = {
      ...current,
      version: result.draft_version,
      subject: editSubject,
      note: editNote,
      email_status: sendState.value === 'unknown' ? 'unknown' : 'failed',
      email_error: result.error,
    }
    patchRow(row.sponsor_id, {
      email_status: sendState.value === 'unknown' ? 'unknown' : 'failed',
      email_error: result.error,
      draft_version: result.draft_version,
    })
  } catch (failure) {
    const uncertain = failure instanceof ApiError && failure.status === 0
    sendState.value = uncertain ? 'unknown' : 'failed'
    sendError.value = uncertain
      ? UNKNOWN_SEND_MESSAGE
      : failureMessage(failure, 'The packet email could not be sent.')
  } finally {
    sending.value = false
  }
}

onMounted(refresh)
onUnmounted(() => {
  ++draftRequest
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
})
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1 style="margin: 0">Dispatch</h1>
      <button type="button" :disabled="loading" @click="refresh">Refresh</button>
    </div>
    <p class="muted">
      Generate child info sheets and sponsor packets, then review and send each sponsor's packet email.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>

    <div v-if="activeJob" class="card status-line" data-testid="active-generation-job">
      <strong>Generate job #{{ activeJob.id }}</strong>
      <span>{{ jobProgress?.phase || activeJob.status }}</span>
      <span>
        {{ jobProgress?.completed ?? 0 }} / {{ jobProgress?.total ?? '—' }}
      </span>
      <RouterLink :to="`/jobs/${activeJob.id}`">Open job</RouterLink>
    </div>

    <p v-if="notice" class="notice" role="status" data-testid="send-notice">
      {{ notice }}
    </p>

    <p v-if="!seasonName" class="muted">No active season loaded.</p>
    <p v-else-if="loading && !data" role="status">Loading dispatch work list…</p>
    <p v-else-if="error" class="error-text" role="alert" data-testid="dispatch-error">
      {{ error }}
      <button type="button" @click="load">Retry</button>
    </p>

    <template v-else-if="data">
      <DispatchGenerationBar
        ref="generationBar"
        :totals="data.totals"
        :enqueueing="enqueueing"
        :error="generationError"
        :conflict-job-id="conflictJobId"
        @run="enqueueGenerate"
      />

      <p v-if="!data.mail_configured" class="muted" data-testid="mail-not-configured">
        The mail account is not configured. Drafts can be reviewed, but packet email cannot be sent.
      </p>

      <p v-if="data.rows.length === 0" class="card" data-testid="dispatch-empty">
        No sponsor has assigned children yet. Run matching or assign families first.
        <RouterLink to="/match">Open matching</RouterLink>
      </p>
      <DispatchSponsorTable
        v-else
        :rows="data.rows"
        :season="seasonName"
        :children="children"
        :can-send="canSend"
        :mail-configured="data.mail_configured"
        style="margin-top: 1rem"
        @open-email="openEmail"
        @generate-packet="generatePacket"
      />
    </template>

    <SponsorEmailModal
      :open="emailOpen"
      :row="selectedRow"
      :draft="draft"
      :loading="draftLoading"
      :error="draftError"
      :saving="saving"
      :sending="sending"
      :send-state="sendState"
      :send-error="sendError"
      :can-send="canSend"
      @close="closeEmail"
      @reload="reloadDraft"
      @save="saveDraft"
      @send="sendEmail"
    />
  </section>
</template>
