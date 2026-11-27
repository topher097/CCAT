<script setup lang="ts">
/** One pipeline command form with exact CLI option semantics (§7.1.8). */

import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import { enqueueJob, type Job } from '../api/jobs'
import { getRunSettings, saveRunSettings } from '../api/runs'
import { useSessionStore } from '../stores/session'
import ConfirmDialog from './ConfirmDialog.vue'
import { useJobPopup } from '../composables/useJobPopup'

const props = withDefaults(
  defineProps<{
    command: 'acquire' | 'screen' | 'generate' | 'stats'
    submitLabel?: string
    /** Card title; defaults to "<Command> run". */
    heading?: string
    /** Collapse to the title until the user opens the card. */
    collapsible?: boolean
  }>(),
  { submitLabel: 'Enqueue run' },
)

const emit = defineEmits<{ enqueued: [job: Job] }>()

const { openJob } = useJobPopup()
const session = useSessionStore()

const test = ref(false)
const overwrite = ref(false)
const skipEmail = ref(false)
const skipAddress = ref(false)
const redo = ref('')
const sponsor = ref('')
const maxWorkers = ref('')
const prepared = ref(false)

/** The global AI screening flag: overwrite the values of "new" children. */
const overwriteNew = ref(false)
/** Whether the caller may write the global flag. */
const canEditSettings = ref(false)
/** Whether the global flag is currently being written. */
const savingSettings = ref(false)
/** Message shown when the global flag cannot be read or written. */
const settingsError = ref<string | null>(null)

const confirmOpen = ref(false)
const enqueueing = ref(false)
const error = ref<string | null>(null)
const conflictJobId = ref<number | null>(null)

const SCOPE: Record<string, string> = {
  acquire: 'Replaces the raw sponsor/family/child snapshot from Zite. Final rows are never deleted or rebuilt.',
  screen: 'Prepares raw records, reconciles final rows, runs email/address checks, and AI-screens uncompleted children.',
  generate: 'Renders child info sheets, then builds sponsor packets from completed sheets.',
  stats: 'Builds the statistics report as HTML and PDF artifacts.',
}

/** Stringify a bound input value (number inputs arrive as numbers). */
function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** Cache-busting label for the current command. */
const title = computed(
  () => props.command.charAt(0).toUpperCase() + props.command.slice(1),
)

/** The card title; the default keeps the old "<Command> run" wording. */
const heading = computed(() => props.heading ?? `${title.value} run`)

/** A collapsible card starts folded to its title. */
const open = ref(false)

/** Whether this command carries the global AI screening flag. */
const hasGlobalAiFlag = computed(() => props.command === 'screen')

/** Load the global AI screening flag and the caller's right to change it. */
async function loadSettings(): Promise<void> {
  if (!hasGlobalAiFlag.value) return
  try {
    const view = await getRunSettings('screen')
    overwriteNew.value = view.run?.overwrite_new === true
    canEditSettings.value = view.editable
  } catch {
    // A settings read failure leaves the flag off and the box
    // disabled; it must not masquerade as a run failure.
    settingsError.value = 'Could not read the screening setting.'
  }
}

onMounted(() => {
  void loadSettings()
})

/**
 * Write the global AI screening flag to the configuration file.
 *
 * A failed write reverts the checkbox: the page must not show a
 * setting the configuration file does not hold.
 */
async function onOverwriteNewChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const wanted = input.checked
  savingSettings.value = true
  settingsError.value = null
  try {
    const view = await saveRunSettings('screen', { overwrite_new: wanted })
    overwriteNew.value = view.run?.overwrite_new === true
    canEditSettings.value = view.editable
  } catch (failure) {
    overwriteNew.value = !wanted
    input.checked = !wanted
    settingsError.value =
      failure instanceof ApiError
        ? failure.message
        : 'Could not save the screening setting.'
  } finally {
    savingSettings.value = false
  }
}

/** Exact payload mirroring the CLI flags (§7.1.8). */
const options = computed<Record<string, unknown>>(() => {
  switch (props.command) {
    case 'screen':
      return {
        test: test.value,
        overwrite: overwrite.value,
        skip_email_check: skipEmail.value,
        skip_address_check: skipAddress.value,
        ...(textOf(redo.value).trim() ? { redo: textOf(redo.value).trim() } : {}),
      }
    case 'generate':
      return {
        test: test.value,
        overwrite: overwrite.value,
        ...(textOf(sponsor.value).trim()
          ? { sponsor: Number(textOf(sponsor.value)) }
          : {}),
        ...(textOf(maxWorkers.value).trim()
          ? { max_workers: Number(textOf(maxWorkers.value)) }
          : {}),
      }
    case 'stats':
      return { raw: prepared.value }
    default:
      return {}
  }
})

/** Plain-language confirmation of scope and mutation. */
const confirmMessage = computed(() => {
  const enabled = Object.entries(options.value)
    .filter(([, value]) => value === true)
    .map(([key]) => key.replaceAll('_', '-'))
  const extra = enabled.length
    ? ` Options: ${enabled.join(', ')}.`
    : ''
  const testNote =
    options.value.test === true
      ? ' Test mode limits scope to the first unit of work but STILL mutates persistent state.'
      : ''
  return `${SCOPE[props.command]} The run is queued as a durable job.${extra}${testNote}`
})

function submit(): void {
  error.value = null
  conflictJobId.value = null
  confirmOpen.value = true
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

async function enqueue(): Promise<void> {
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
    const job = await enqueueJob(
      season,
      props.command,
      options.value,
    )
    emit('enqueued', job)
    openJob(job.id)
  } catch (failure) {
    if (!(failure instanceof ApiError)) {
      error.value = 'Enqueue failed.'
      return
    }
    const details = fieldErrors(failure.context)
    error.value = details ? `${failure.message}: ${details}` : failure.message
    if (failure.status === 409) {
      const match = /#(\d+)/.exec(failure.message)
      conflictJobId.value = match ? Number(match[1]) : null
    }
  } finally {
    enqueueing.value = false
  }
}
</script>

<template>
  <form class="card" data-testid="run-form" @submit.prevent="submit">
    <button
      v-if="props.collapsible"
      type="button"
      class="run-toggle"
      :aria-expanded="open"
      data-testid="run-toggle"
      @click="open = !open"
    >
      <span>{{ heading }}</span>
      <span class="run-toggle-chevron" aria-hidden="true">{{
        open ? '▾' : '▸'
      }}</span>
    </button>
    <h2 v-else style="margin-top: 0">{{ heading }}</h2>
    <div v-if="!props.collapsible || open">
      <p class="muted">{{ SCOPE[props.command] }}</p>

    <fieldset
      v-if="props.command !== 'acquire'"
      style="border: 0; padding: 0; margin: 0 0 0.75rem"
    >
      <legend class="muted">Options</legend>

      <label v-if="props.command !== 'stats'" style="display: block">
        <input v-model="test" type="checkbox" name="test" />
        Test mode (first unit of work only)
      </label>
      <p
        v-if="props.command !== 'stats'"
        class="muted"
        data-testid="test-warning"
      >
        Test mode limits scope to the first unit of work but STILL mutates
        persistent state.
      </p>

      <label v-if="props.command !== 'stats'" style="display: block">
        <input v-model="overwrite" type="checkbox" name="overwrite" />
        Overwrite completed work
      </label>
      <p
        v-if="props.command !== 'stats'"
        class="muted"
        data-testid="overwrite-warning"
      >
        Overwrite re-does work that is already done.
      </p>

      <label v-if="props.command === 'screen'" style="display: block">
        <input v-model="skipEmail" type="checkbox" name="skip_email_check" />
        Skip email deliverability check
      </label>
      <label v-if="props.command === 'screen'" style="display: block">
        <input
          v-model="skipAddress"
          type="checkbox"
          name="skip_address_check"
        />
        Skip family address validation
      </label>
      <label v-if="props.command === 'screen'" style="display: block">
        Re-screen one child (record id)
        <input v-model="redo" type="text" name="redo" />
      </label>

      <fieldset
        v-if="props.command === 'screen'"
        style="border: 0; padding: 0; margin: 0 0 0.75rem"
        data-testid="screen-global-settings"
      >
        <legend class="muted">AI screening setting (global)</legend>
        <label style="display: block">
          <input
            :checked="overwriteNew"
            type="checkbox"
            name="overwrite_new"
            data-testid="overwrite-new"
            :disabled="!canEditSettings || savingSettings"
            @change="onOverwriteNewChange"
          />
          Overwrite existing values of new children
        </label>
        <p class="muted" data-testid="overwrite-new-warning">
          Off by default: a human edit to a new child survives re-screening.
          On: the AI replaces every value of a child whose status is still
          "new". Saved to the configuration file, so it applies to every
          screening run.
        </p>
        <p v-if="!canEditSettings" class="muted" data-testid="read-only">
          Read-only: you need the coordinator or admin role to change this
          setting.
        </p>
        <p
          v-if="settingsError"
          class="error-text"
          role="alert"
          data-testid="settings-error"
        >
          {{ settingsError }}
        </p>
      </fieldset>

      <label v-if="props.command === 'generate'" style="display: block">
        Restrict to one sponsor id
        <input v-model="sponsor" type="number" min="1" name="sponsor" />
      </label>
      <label v-if="props.command === 'generate'" style="display: block">
        Rendering workers
        <input
          v-model="maxWorkers"
          type="number"
          min="1"
          name="max_workers"
        />
      </label>

      <label v-if="props.command === 'stats'" style="display: block">
        <input v-model="prepared" type="checkbox" name="prepared" />
        Prepared source mode (ephemeral cleaned snapshot)
      </label>
    </fieldset>

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

    <button type="submit" class="primary" :disabled="enqueueing">
      {{ enqueueing ? 'Enqueuing…' : props.submitLabel }}
    </button>

    <ConfirmDialog
      :open="confirmOpen"
      :title="heading"
      :message="confirmMessage"
      :confirm-label="props.submitLabel"
      :dangerous="true"
      @confirm="enqueue"
      @cancel="confirmOpen = false"
    />
    </div>
  </form>
</template>

<style scoped>
/* The collapsed card shows only its title; the chevron signals state. */
.run-toggle {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  border: 0;
  background: none;
  padding: 0;
  font: inherit;
  font-size: 1.25rem;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}

.run-toggle-chevron {
  font-size: 0.9rem;
  color: #6b7a88;
}
</style>
