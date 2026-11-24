<script setup lang="ts">
/** One command's run button and pop-up settings menu (D12, §11.8). */

import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useJobPopup } from '../composables/useJobPopup'

import { ApiError } from '../api/client'
import { enqueueJob, type Job } from '../api/jobs'
import {
  getRunSettings,
  saveRunSettings,
  type GenerateRunSettings,
  type MatchRunSettings,
  type RunCommand,
} from '../api/runs'
import { useSessionStore } from '../stores/session'
import ConfirmDialog from './ConfirmDialog.vue'

const props = withDefaults(
  defineProps<{
    command: RunCommand
    /** The per-run scope for `generate`; never remembered (D13). */
    sponsor?: number | null
    submitLabel?: string
  }>(),
  { sponsor: null, submitLabel: '' },
)

const emit = defineEmits<{ enqueued: [job: Job] }>()

const LABELS: Record<RunCommand, string> = {
  match: 'Run automatic match',
  generate: 'Regenerate sheets and packets',
}

const SCOPE: Record<RunCommand, string> = {
  match:
    'Matches audited children to sponsors, commits assignments, and writes a match report.',
  generate:
    'Renders child information sheets, then builds sponsor packets from completed sheets.',
}

const { openJob } = useJobPopup()
const session = useSessionStore()

const root = ref<HTMLDivElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const loading = ref(false)
const editable = ref(false)

const test = ref(false)
const skipMatch = ref(false)
const overwrite = ref(false)
const maxWorkers = ref('')
const sponsor = ref(props.sponsor === null ? '' : String(props.sponsor))
const remember = ref(true)

const confirmOpen = ref(false)
const enqueueing = ref(false)
const error = ref<string | null>(null)
const conflictJobId = ref<number | null>(null)

const label = computed(
  () => props.submitLabel || LABELS[props.command],
)

/** Stringify a bound input value (number inputs arrive as numbers). */
function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** Worker count with the model's default when the field is blank. */
function workerCount(): number {
  const value = Number(textOf(maxWorkers.value))
  return Number.isFinite(value) && value >= 1 ? value : 4
}

/** What the command's `run` section receives (never the sponsor scope). */
const rememberable = computed<MatchRunSettings | GenerateRunSettings>(() =>
  props.command === 'match'
    ? { test: test.value, skip_match: skipMatch.value }
    : {
        test: test.value,
        overwrite: overwrite.value,
        max_workers: workerCount(),
      },
)

/** The exact job options for this run. */
const options = computed<Record<string, unknown>>(() => {
  if (props.command === 'match') {
    return { test: test.value && !skipMatch.value, skip_match: skipMatch.value }
  }
  const scoped = textOf(sponsor.value).trim()
  return {
    test: test.value,
    overwrite: overwrite.value,
    max_workers: workerCount(),
    ...(scoped ? { sponsor: Number(scoped) } : {}),
  }
})

/** Plain-language confirmation of scope and mutation. */
const confirmMessage = computed(() => {
  const entries = Object.entries(options.value).map(
    ([key, value]) => `${key.replaceAll('_', '-')}: ${value}`,
  )
  const saved =
    remember.value && editable.value
      ? ` The settings are saved to ${props.command === 'match' ? 'matchmaking' : 'generate'}.run.`
      : ''
  const testNote =
    options.value.test === true
      ? ' Test mode limits scope to the first unit of work but STILL mutates persistent state.'
      : ''
  return `${SCOPE[props.command]} Options — ${entries.join(', ')}.${saved} The run is queued as a durable job.${testNote}`
})

// The runtime ignores test mode with skip matching, and the config contract
// rejects the pair.
watch(skipMatch, (on) => {
  if (on) test.value = false
})

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

function showFailure(failure: unknown): void {
  if (!(failure instanceof ApiError)) {
    error.value = 'Request failed.'
    return
  }
  const details = fieldErrors(failure.context)
  error.value = details ? `${failure.message}: ${details}` : failure.message
  if (failure.status === 409) {
    const match = /#(\d+)/.exec(failure.message)
    conflictJobId.value = match ? Number(match[1]) : null
  }
}

async function openMenu(): Promise<void> {
  open.value = true
  error.value = null
  conflictJobId.value = null
  loading.value = true
  try {
    const view = await getRunSettings(props.command)
    editable.value = view.editable
    const run = view.run ?? {}
    test.value = run.test === true
    skipMatch.value = run.skip_match === true
    overwrite.value = run.overwrite === true
    maxWorkers.value =
      run.max_workers === null || run.max_workers === undefined
        ? ''
        : String(run.max_workers)
    remember.value = true
  } catch (failure) {
    showFailure(failure)
  } finally {
    loading.value = false
  }
}

function closeMenu(returnFocus = false): void {
  open.value = false
  if (returnFocus) trigger.value?.focus()
}

function toggle(): void {
  if (open.value) closeMenu(true)
  else void openMenu()
}

function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  event.preventDefault()
  closeMenu(true)
}

function onDocumentPointerDown(event: MouseEvent): void {
  if (!root.value?.contains(event.target as Node)) closeMenu(false)
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('keydown', onDocumentKeydown)
    document.addEventListener('mousedown', onDocumentPointerDown)
  } else {
    document.removeEventListener('keydown', onDocumentKeydown)
    document.removeEventListener('mousedown', onDocumentPointerDown)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onDocumentKeydown)
  document.removeEventListener('mousedown', onDocumentPointerDown)
})

function submit(): void {
  error.value = null
  conflictJobId.value = null
  confirmOpen.value = true
}

async function execute(): Promise<void> {
  confirmOpen.value = false
  const season = session.season?.name
  if (!season) {
    error.value = 'No active season loaded.'
    return
  }
  enqueueing.value = true
  error.value = null
  conflictJobId.value = null
  try {
    if (remember.value && editable.value) {
      try {
        await saveRunSettings(props.command, rememberable.value)
      } catch (failure) {
        // A failed write aborts the run: never enqueue with stale defaults.
        showFailure(failure)
        return
      }
    }
    const job = await enqueueJob(
      season,
      props.command,
      options.value,
      crypto.randomUUID(),
    )
    emit('enqueued', job)
    openJob(job.id)
  } catch (failure) {
    showFailure(failure)
  } finally {
    enqueueing.value = false
  }
}
</script>

<template>
  <div ref="root" class="run-menu">
    <button
      ref="trigger"
      type="button"
      class="run-menu-trigger"
      aria-haspopup="menu"
      :aria-expanded="open"
      :aria-controls="`${props.command}-run-menu`"
      data-testid="run-trigger"
      :disabled="enqueueing"
      @click="toggle"
    >
      <span aria-hidden="true">▾</span> {{ label }}
    </button>

    <div
      v-if="open"
      :id="`${props.command}-run-menu`"
      class="run-menu-panel card"
      role="menu"
      :aria-label="label"
      data-testid="run-menu"
    >
      <p class="muted" style="margin-top: 0">{{ SCOPE[props.command] }}</p>
      <p v-if="props.command === 'match'" class="muted" data-testid="manual-protection">
        Families with a manual assignment are protected from the automatic
        run.
      </p>

      <p v-if="loading" role="status">Loading run settings…</p>
      <template v-else>
        <p v-if="!editable" class="muted" data-testid="read-only">
          Read-only: you need the coordinator or admin role to run this
          command.
        </p>

        <label style="display: block">
          <input
            v-model="test"
            type="checkbox"
            name="test"
            role="menuitemcheckbox"
            :aria-checked="test"
            :disabled="!editable || skipMatch"
          />
          Test mode (first unit of work only)
        </label>
        <p class="muted">
          Test mode limits scope to the first unit of work but STILL mutates
          persistent state.
        </p>

        <template v-if="props.command === 'match'">
          <label style="display: block">
            <input
              v-model="skipMatch"
              type="checkbox"
              name="skip_match"
              role="menuitemcheckbox"
              :aria-checked="skipMatch"
              :disabled="!editable"
            />
            Skip matching; reconcile existing assignments only
          </label>
          <p class="muted">Test mode has no effect with skip matching on.</p>
        </template>

        <template v-else>
          <label style="display: block">
            <input
              v-model="overwrite"
              type="checkbox"
              name="overwrite"
              role="menuitemcheckbox"
              :aria-checked="overwrite"
              :disabled="!editable"
            />
            Overwrite completed work
          </label>
          <p class="muted">Re-renders sheets that already exist.</p>

          <label style="display: block">
            Rendering workers
            <input
              v-model="maxWorkers"
              type="number"
              min="1"
              name="max_workers"
              :disabled="!editable"
            />
          </label>

          <label style="display: block">
            Limit to one sponsor id
            <input
              v-model="sponsor"
              type="number"
              min="1"
              name="sponsor"
              :disabled="!editable"
            />
          </label>
          <p class="muted">Scopes this run only; never remembered.</p>
        </template>

        <label style="display: block">
          <input
            v-model="remember"
            type="checkbox"
            name="remember"
            role="menuitemcheckbox"
            :aria-checked="remember"
            :disabled="!editable"
          />
          Remember these settings
        </label>
        <p class="muted">
          Saves them to
          {{ props.command === 'match' ? 'matchmaking' : 'generate' }}.run in
          the configuration file.
        </p>

        <p v-if="error" class="error-text" role="alert" data-testid="run-error">
          {{ error }}
          <button
            v-if="conflictJobId !== null"
            type="button"
            @click="openJob(conflictJobId)"
            data-testid="conflict-job-link"
          >
            Open active job #{{ conflictJobId }}
          </button>
        </p>

        <div class="dialog-actions">
          <button type="button" role="menuitem" @click="closeMenu(true)">
            Cancel
          </button>
          <button
            v-if="editable"
            type="button"
            role="menuitem"
            class="primary"
            data-testid="run-confirm"
            :disabled="enqueueing"
            @click="submit"
          >
            {{ enqueueing ? 'Enqueuing…' : label }}
          </button>
        </div>
      </template>
    </div>

    <ConfirmDialog
      :open="confirmOpen"
      :title="label"
      :message="confirmMessage"
      :confirm-label="label"
      :dangerous="true"
      @confirm="execute"
      @cancel="confirmOpen = false"
    />
  </div>
</template>

<style scoped>
.run-menu {
  position: relative;
  display: inline-block;
}

.run-menu-panel {
  position: absolute;
  top: calc(100% + 0.25rem);
  left: 0;
  z-index: 10;
  min-width: 20rem;
  max-width: 26rem;
}

.run-menu-panel label {
  margin-bottom: 0.25rem;
}
</style>
