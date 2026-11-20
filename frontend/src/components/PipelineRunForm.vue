<script setup lang="ts">
/** One pipeline command form with exact CLI option semantics (§7.1.8). */

import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { ApiError } from '../api/client'
import { enqueueJob, type Job } from '../api/jobs'
import { useSessionStore } from '../stores/session'
import ConfirmDialog from './ConfirmDialog.vue'

const props = withDefaults(
  defineProps<{
    command: 'acquire' | 'screen' | 'match' | 'generate' | 'stats'
    submitLabel?: string
  }>(),
  { submitLabel: 'Enqueue run' },
)

const emit = defineEmits<{ enqueued: [job: Job] }>()

const router = useRouter()
const session = useSessionStore()

const test = ref(false)
const overwrite = ref(false)
const skipEmail = ref(false)
const skipAddress = ref(false)
const skipMatch = ref(false)
const redo = ref('')
const sponsor = ref('')
const maxWorkers = ref('')
const prepared = ref(false)

const confirmOpen = ref(false)
const enqueueing = ref(false)
const error = ref<string | null>(null)
const conflictJobId = ref<number | null>(null)

const SCOPE: Record<string, string> = {
  acquire: 'Replaces the raw sponsor/family/child snapshot from Zite. Final rows are never deleted or rebuilt.',
  screen: 'Prepares raw records, reconciles final rows, runs email/address checks, and AI-screens uncompleted children.',
  match: 'Matches audited children to sponsors, commits assignments, and writes a match report.',
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
    case 'match':
      return {
        test: test.value,
        overwrite: overwrite.value,
        skip_match: skipMatch.value,
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
    await router.push(`/jobs/${job.id}`)
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
    <h2 style="margin-top: 0">{{ title }} run</h2>
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

      <label v-if="props.command === 'match'" style="display: block">
        <input v-model="skipMatch" type="checkbox" name="skip_match" />
        Skip matching; reconcile existing assignments only
      </label>

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
      <RouterLink
        v-if="conflictJobId !== null"
        :to="`/jobs/${conflictJobId}`"
        data-testid="conflict-job-link"
      >
        Open active job #{{ conflictJobId }}
      </RouterLink>
    </p>

    <button type="submit" class="primary" :disabled="enqueueing">
      {{ enqueueing ? 'Enqueuing…' : props.submitLabel }}
    </button>

    <ConfirmDialog
      :open="confirmOpen"
      :title="`${title} run`"
      :message="confirmMessage"
      :confirm-label="props.submitLabel"
      :dangerous="true"
      @confirm="enqueue"
      @cancel="confirmOpen = false"
    />
  </form>
</template>
