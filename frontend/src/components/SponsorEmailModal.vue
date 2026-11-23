<script setup lang="ts">
/** Sandboxed packet-email preview with optimistic draft editing and sending. */

import { computed, nextTick, ref, watch } from 'vue'

import type {
  DispatchDraft,
  DispatchDraftRequest,
  DispatchRow,
} from '../api/dispatch'
import { artifactDownloadUrl } from '../api/commands'
import { formatTime } from '../utils/format'
import { personName } from '../utils/sponsor'
import ConfirmDialog from './ConfirmDialog.vue'

const props = defineProps<{
  open: boolean
  row: DispatchRow | null
  draft: DispatchDraft | null
  loading?: boolean
  error?: string | null
  saving?: boolean
  sending?: boolean
  sendState?: 'failed' | 'unknown' | null
  sendError?: string | null
  canSend: boolean
}>()

const emit = defineEmits<{
  close: []
  reload: []
  save: [edit: DispatchDraftRequest]
  send: [edit: DispatchDraftRequest]
}>()

const dialog = ref<HTMLDivElement | null>(null)
const subject = ref('')
const note = ref('')
const duplicateConfirm = ref(false)
let opener: HTMLElement | null = null

const readOnly = computed(
  () => props.row?.email_status === 'sent' || !props.canSend,
)
const dirty = computed(
  () =>
    props.draft !== null &&
    (subject.value !== props.draft.subject || note.value !== props.draft.note),
)
const title = computed(() =>
  props.row
    ? `Email sponsor #${props.row.sponsor_id} — ${personName(props.row)}`
    : 'Sponsor email',
)
const fromLabel = computed(() => {
  const value = props.draft?.from_email
  return typeof value === 'string' && value ? value : 'Configured mail account'
})
const duplicateNames = computed(() =>
  (props.row?.duplicate_recipient_sponsor_ids ?? [])
    .map((id) => `#${id}`)
    .join(', '),
)

watch(
  () => props.draft,
  (draft) => {
    subject.value = draft?.subject ?? ''
    note.value = draft?.note ?? ''
  },
  { immediate: true },
)

watch(
  () => props.open,
  async (open) => {
    if (open) {
      opener = document.activeElement as HTMLElement | null
      await nextTick()
      dialog.value?.focus()
    } else {
      duplicateConfirm.value = false
      opener?.focus()
      opener = null
    }
  },
  { immediate: true },
)

function requestClose(): void {
  if (props.sending) return
  if (dirty.value && !readOnly.value && !window.confirm('Discard unsaved changes?')) {
    return
  }
  emit('close')
}

function onKeydown(event: KeyboardEvent): void {
  if ((event.target as HTMLElement).closest('.dialog') !== dialog.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    requestClose()
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return
  const focusable = dialog.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )
  if (focusable.length === 0) return
  const first = focusable[0]!
  const last = focusable[focusable.length - 1]!
  const active = document.activeElement
  if (event.shiftKey && (active === first || active === dialog.value)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

function save(): void {
  emit('save', { subject: subject.value, note: note.value })
}

function requestSend(): void {
  if (props.row?.duplicate_recipient_sponsor_ids.length) {
    duplicateConfirm.value = true
    return
  }
  emit('send', { subject: subject.value, note: note.value })
}

function confirmDuplicateSend(): void {
  duplicateConfirm.value = false
  emit('send', { subject: subject.value, note: note.value })
}

function sizeLabel(bytes: number): string {
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}
</script>

<template>
  <div v-if="props.open" class="dialog-backdrop" @click.self="requestClose">
    <div
      ref="dialog"
      class="dialog email-modal"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      :aria-label="title"
      data-testid="sponsor-email-modal"
      @keydown="onKeydown"
    >
      <div class="status-line" style="justify-content: space-between">
        <h2 style="margin: 0">{{ title }}</h2>
        <span v-if="props.row" class="badge status-in-progress">
          {{ props.row.email_status.replaceAll('_', ' ') }}
        </span>
      </div>
      <p v-if="props.row?.email_sent_at" class="muted">
        Sent {{ formatTime(props.row.email_sent_at) }}
      </p>

      <p v-if="props.loading" role="status">Loading draft…</p>
      <p v-else-if="props.error && !props.draft" class="error-text" role="alert">
        {{ props.error }}
        <button type="button" :disabled="props.sending" @click="emit('reload')">
          Retry
        </button>
      </p>

      <template v-else-if="props.draft && props.row">
        <dl class="envelope">
          <div><dt>From</dt><dd>{{ fromLabel }}</dd></div>
          <div><dt>To</dt><dd data-testid="draft-recipient">{{ props.draft.to_email }}</dd></div>
          <div><dt>BCC</dt><dd>{{ props.draft.bcc_emails.join(', ') || '—' }}</dd></div>
        </dl>

        <label class="subject-label">
          Subject
          <input
            v-model="subject"
            type="text"
            name="subject"
            data-testid="draft-subject"
            :readonly="readOnly"
            :disabled="props.sending"
          />
        </label>

        <section aria-label="Email attachments">
          <h3>Attachments</h3>
          <p v-if="props.draft.attachments.length === 0" class="muted">
            No attachments.
          </p>
          <ul v-else data-testid="draft-attachments">
            <li v-for="attachment in props.draft.attachments" :key="`${attachment.role}-${attachment.filename}`">
              <a
                v-if="attachment.artifact_id !== null && attachment.artifact_id !== undefined"
                :href="artifactDownloadUrl(attachment.artifact_id)"
              >
                {{ attachment.filename }}
              </a>
              <span v-else>{{ attachment.filename }}</span>
              <span class="muted"> ({{ sizeLabel(attachment.size_bytes) }})</span>
            </li>
          </ul>
        </section>

        <section aria-label="Email body preview">
          <h3>Body preview</h3>
          <iframe
            class="body-preview"
            title="Sponsor email body preview"
            sandbox=""
            :srcdoc="props.draft.body_html"
            data-testid="draft-body"
          ></iframe>
        </section>

        <label class="note-label">
          Note (optional, plain text)
          <textarea
            v-model="note"
            name="note"
            rows="4"
            data-testid="draft-note"
            :readonly="readOnly"
            :disabled="props.sending"
          ></textarea>
        </label>
        <p class="muted">
          The preview shows the body as drafted; the server inserts this note when sending.
        </p>

        <ul v-if="props.draft.blockers.length" class="error-text" data-testid="draft-blockers">
          <li v-for="blocker in props.draft.blockers" :key="blocker">
            {{ blocker.replaceAll('_', ' ') }}
          </li>
        </ul>
        <p v-if="props.error" class="error-text" role="alert">{{ props.error }}</p>
        <p
          v-if="props.sendError"
          class="error-text"
          role="alert"
          data-testid="send-error"
        >
          {{ props.sendError }}
        </p>

        <div class="dialog-actions">
          <button
            v-if="!readOnly"
            type="button"
            data-testid="save-draft"
            :disabled="props.saving || props.sending || !dirty"
            @click="save"
          >
            {{ props.saving ? 'Saving…' : 'Save draft' }}
          </button>
          <button type="button" :disabled="props.sending" @click="requestClose">
            {{ readOnly ? 'Close' : 'Cancel' }}
          </button>
          <button
            v-if="!readOnly && props.sendState !== 'unknown'"
            type="button"
            class="primary"
            data-testid="send-email"
            :disabled="props.sending || props.saving || props.draft.blockers.length > 0"
            @click="requestSend"
          >
            {{
              props.sending
                ? 'Sending…'
                : props.sendState === 'failed'
                  ? 'Retry send'
                  : 'Send email'
            }}
          </button>
        </div>
      </template>

      <div v-else class="dialog-actions">
        <button type="button" :disabled="props.sending" @click="requestClose">
          Close
        </button>
      </div>

      <ConfirmDialog
        :open="duplicateConfirm"
        title="Duplicate recipient address"
        :message="`This address is also used by sponsor ${duplicateNames}. Confirm that this packet email should be sent.`"
        confirm-label="Send email"
        @confirm="confirmDuplicateSend"
        @cancel="duplicateConfirm = false"
      />
    </div>
  </div>
</template>

<style scoped>
.email-modal {
  width: min(60rem, 94vw);
  max-width: none;
  max-height: 92vh;
  overflow-y: auto;
  outline: none;
}

.envelope {
  margin: 0.8rem 0;
}

.envelope > div {
  display: grid;
  grid-template-columns: 4rem minmax(0, 1fr);
  gap: 0.5rem;
}

.envelope dt {
  font-weight: 600;
}

.envelope dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.subject-label,
.note-label {
  display: block;
  margin-top: 0.75rem;
  font-weight: 600;
}

.subject-label input,
.note-label textarea {
  display: block;
  width: 100%;
  margin-top: 0.3rem;
  padding: 0.4rem 0.5rem;
  border: 1px solid #9aa7b4;
  border-radius: 4px;
  font: inherit;
}

.body-preview {
  display: block;
  width: 100%;
  min-height: 20rem;
  border: 1px solid #d7dee6;
  background: #fff;
}
</style>
