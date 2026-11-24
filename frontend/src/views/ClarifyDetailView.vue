<script setup lang="ts">
/** /clarify/:familyId — draft edit, contact toggles, safe text preview. */

import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  enqueueClarifyExport,
  getClarification,
  patchDraft,
  setContactFlag,
  type ClarifyDraft,
  type ClarifyFamily,
  type DraftParagraph,
} from '../api/clarify'
import { enqueueJob } from '../api/jobs'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import type { ConflictInfo } from '../utils/conflict'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { useJobPopup } from '../composables/useJobPopup'
import ConflictPanel from '../components/ConflictPanel.vue'

const route = useRoute()
const { openJob } = useJobPopup()
const session = useSessionStore()

const familyId = computed(() => String(route.params.familyId ?? ''))
const seasonName = computed(() => session.season?.name ?? '')

const family = ref<ClarifyFamily | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)

const editing = ref(false)
const saving = ref(false)
const saveError = ref<string | null>(null)
const savedMessage = ref<string | null>(null)
const touched = ref<Set<string>>(new Set())
const editable = ref({
  subject: '',
  greeting: '',
  intro: '',
  closing: '',
})
const paragraphText = ref<Record<string, string>>({})
const paragraphOrder = ref<string[]>([])
const draftVersion = ref(0)
const conflict = ref<ConflictInfo | null>(null)

const contactBusy = ref<'contacted' | 'replied' | null>(null)
const contactError = ref<string | null>(null)

const regenerateOpen = ref(false)
const exportOpen = ref(false)
const jobBusy = ref(false)
const jobError = ref<string | null>(null)

const draft = computed(() => family.value?.draft ?? null)
const children = computed(
  () => (family.value?.children ?? []) as Record<string, unknown>[],
)
const contactEmail = computed(() =>
  String(children.value[0]?.family_email ?? '').trim(),
)
const contactPhone = computed(() =>
  String(children.value[0]?.family_phone ?? '').trim(),
)

const LABELS: Record<string, string> = {
  subject: 'Subject',
  greeting: 'Greeting',
  intro: 'Intro',
  closing: 'Closing',
  paragraphs_text: 'Per-child paragraphs',
}

function draftParagraphs(value: ClarifyDraft | null): DraftParagraph[] {
  const rows = (value?.paragraphs ?? []) as DraftParagraph[]
  return [...rows].sort((a, b) => Number(a.order) - Number(b.order))
}

/** Load one draft into the editable copy (clears touched state). */
function applyDraft(value: ClarifyDraft): void {
  draftVersion.value = value.version
  const paragraphs = draftParagraphs(value)
  paragraphOrder.value = paragraphs.map((row) => String(row.zite_record_id))
  const texts: Record<string, string> = {}
  for (const row of paragraphs) {
    texts[String(row.zite_record_id)] = String(row.text ?? '')
  }
  paragraphText.value = texts
  editable.value = {
    subject: value.subject ?? '',
    greeting: value.greeting ?? '',
    intro: value.intro ?? '',
    closing: value.closing ?? '',
  }
  touched.value = new Set()
}

/** Flatten a server draft for the conflict diff. */
function flattenServer(value: ClarifyDraft): Record<string, unknown> {
  return {
    subject: value.subject ?? '',
    greeting: value.greeting ?? '',
    intro: value.intro ?? '',
    closing: value.closing ?? '',
    paragraphs_text: draftParagraphs(value)
      .map((row) => String(row.text ?? ''))
      .join('\n\n'),
  }
}

/** Flatten the local edit for the conflict diff. */
function flattenLocal(): Record<string, unknown> {
  return {
    subject: editable.value.subject,
    greeting: editable.value.greeting,
    intro: editable.value.intro,
    closing: editable.value.closing,
    paragraphs_text: paragraphOrder.value
      .map((id) => paragraphText.value[id] ?? '')
      .join('\n\n'),
  }
}

function childLabel(id: string): string {
  const row = children.value.find(
    (child) => String(child.zite_record_id) === id,
  )
  return String(row?.first_name ?? row?.angel_id ?? id)
}

function mark(field: string): void {
  const next = new Set(touched.value)
  next.add(field)
  touched.value = next
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    loadError.value = 'No active season loaded.'
    return
  }
  loading.value = true
  loadError.value = null
  try {
    const value = await getClarification(seasonName.value, familyId.value)
    family.value = value
    if (value.draft) applyDraft(value.draft)
    else {
      draftVersion.value = 0
      paragraphOrder.value = []
      paragraphText.value = {}
    }
  } catch (failure) {
    loadError.value =
      failure instanceof Error ? failure.message : 'clarification request failed'
  } finally {
    loading.value = false
  }
}

function buildPatch(): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  if (touched.value.has('subject')) {
    patch.subject = editable.value.subject.trim() || null
  }
  if (touched.value.has('greeting')) {
    patch.greeting = editable.value.greeting.trim() || null
  }
  if (touched.value.has('intro')) {
    patch.intro = editable.value.intro.trim() || null
  }
  if (touched.value.has('closing')) {
    patch.closing = editable.value.closing.trim() || null
  }
  if (touched.value.has('paragraphs')) {
    patch.paragraphs = paragraphOrder.value.map((id, order) => {
      const row = draftParagraphs(draft.value).find(
        (entry) => String(entry.zite_record_id) === id,
      )
      return {
        zite_record_id: id,
        angel_id: row?.angel_id ?? null,
        order,
        text: (paragraphText.value[id] ?? '').trim(),
      }
    })
  }
  return patch
}

async function save(): Promise<void> {
  saveError.value = null
  savedMessage.value = null
  conflict.value = null
  const patch = buildPatch()
  if (Object.keys(patch).length === 0) {
    savedMessage.value = 'No changes to save.'
    return
  }
  saving.value = true
  try {
    const updated = await patchDraft(
      seasonName.value,
      familyId.value,
      patch,
      draftVersion.value,
    )
    if (family.value) family.value = { ...family.value, draft: updated }
    applyDraft(updated)
    editing.value = false
    savedMessage.value = `Saved draft version ${updated.version}.`
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 412) {
      await showConflict(failure)
    } else {
      saveError.value =
        failure instanceof Error ? failure.message : 'draft save failed'
    }
  } finally {
    saving.value = false
  }
}

/** Preserve the local edits and show the fresh server draft (§7.2). */
async function showConflict(error: ApiError): Promise<void> {
  try {
    const fresh = await getClarification(seasonName.value, familyId.value)
    family.value = fresh
    if (!fresh.draft) {
      saveError.value = error.message
      return
    }
    conflict.value = {
      mode: 'edit',
      row: flattenServer(fresh.draft),
      currentVersion: fresh.draft.version,
      lastUpdatedBy: fresh.draft.edited_by ?? null,
      lastUpdatedAt: fresh.draft.edited_at ?? null,
      message: error.message,
    }
  } catch {
    saveError.value = error.message
  }
}

function rebase(): void {
  if (!conflict.value) return
  draftVersion.value = conflict.value.currentVersion
  savedMessage.value = `Rebased onto server version ${conflict.value.currentVersion}; save again to apply your edits.`
  conflict.value = null
}

async function reloadDraft(): Promise<void> {
  conflict.value = null
  editing.value = false
  await load()
}

async function toggle(flag: 'contacted' | 'replied'): Promise<void> {
  if (!family.value) return
  contactBusy.value = flag
  contactError.value = null
  try {
    const updated = await setContactFlag(
      seasonName.value,
      familyId.value,
      flag,
      !family.value[flag],
    )
    family.value = {
      ...family.value,
      contacted: updated.contacted,
      contacted_at: updated.contacted_at,
      replied: updated.replied,
      updated_at: updated.updated_at,
    }
  } catch (failure) {
    contactError.value =
      failure instanceof Error ? failure.message : 'contact update failed'
  } finally {
    contactBusy.value = null
  }
}

async function enqueueAndOpen(
  enqueue: () => Promise<{ id: number }>,
  close: () => void,
): Promise<void> {
  close()
  jobError.value = null
  jobBusy.value = true
  try {
    const job = await enqueue()
    openJob(job.id)
  } catch (failure) {
    jobError.value =
      failure instanceof ApiError
        ? failure.message
        : 'job request failed'
  } finally {
    jobBusy.value = false
  }
}

function regenerate(): void {
  void enqueueAndOpen(
    () => enqueueJob(seasonName.value, 'clarify_generate', { overwrite: true }),
    () => (regenerateOpen.value = false),
  )
}

function exportCsv(): void {
  void enqueueAndOpen(
    () => enqueueClarifyExport(seasonName.value),
    () => (exportOpen.value = false),
  )
}

onMounted(() => void load())
</script>

<template>
  <section>
    <p><RouterLink to="/clarify">← All clarification families</RouterLink></p>

    <p v-if="loading" role="status">Loading clarification family…</p>
    <p
      v-else-if="loadError"
      class="error-text"
      role="alert"
      data-testid="clarify-detail-error"
    >
      {{ loadError }} <button type="button" @click="load">Retry</button>
    </p>

    <template v-if="family">
      <div class="status-line" style="justify-content: space-between">
        <h1 style="margin: 0">Family {{ family.family_id }}</h1>
        <div class="status-line">
          <button
            type="button"
            :disabled="jobBusy"
            data-testid="clarify-export"
            @click="exportOpen = true"
          >
            Export CSV
          </button>
          <button
            type="button"
            :disabled="jobBusy"
            data-testid="regenerate-draft"
            @click="regenerateOpen = true"
          >
            Regenerate draft
          </button>
        </div>
      </div>

      <div class="card" style="margin-top: 0.75rem">
        <h2 style="margin-top: 0">Contact</h2>
        <p v-if="contactEmail || contactPhone">
          <span v-if="contactEmail">{{ contactEmail }}</span>
          <span v-if="contactPhone"> · {{ contactPhone }}</span>
        </p>
        <p v-else class="muted" data-testid="clarify-no-contact">
          No family contact on file — the family record is ignored or missing.
        </p>
        <p v-if="contactError" class="error-text" role="alert">
          {{ contactError }}
        </p>
        <div class="status-line">
          <button
            type="button"
            :aria-pressed="family.contacted"
            :disabled="contactBusy !== null"
            data-testid="toggle-contacted"
            @click="toggle('contacted')"
          >
            Contacted: {{ family.contacted ? 'yes' : 'no' }}
          </button>
          <button
            type="button"
            :aria-pressed="family.replied"
            :disabled="contactBusy !== null"
            data-testid="toggle-replied"
            @click="toggle('replied')"
          >
            Replied: {{ family.replied ? 'yes' : 'no' }}
          </button>
          <span v-if="family.contacted_at" class="muted">
            contacted {{ formatTime(family.contacted_at) }}
          </span>
        </div>
        <p v-if="jobError" class="error-text" role="alert">{{ jobError }}</p>
      </div>

      <section class="card" style="margin-top: 1rem">
        <h2 style="margin-top: 0">Flagged children</h2>
        <p v-if="children.length === 0" class="muted">
          No flagged child rows remain for this family.
        </p>
        <table v-else class="data" data-testid="clarify-children">
          <thead>
            <tr>
              <th>Child</th>
              <th>Name</th>
              <th>Raw DOB</th>
              <th>Notes</th>
              <th>Needs</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in children" :key="String(row.zite_record_id)">
              <td>
                <RouterLink :to="`/audit/children/${row.zite_record_id}`">
                  {{ row.angel_id ?? row.zite_record_id }}
                </RouterLink>
              </td>
              <td>{{ row.first_name ?? '—' }}</td>
              <td>{{ row.raw_dob ?? '—' }}</td>
              <td>{{ row.clarification_notes ?? '—' }}</td>
              <td>
                {{
                  [row.need_1, row.need_2, row.desire_1, row.desire_2]
                    .filter(Boolean)
                    .join(', ') || '—'
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="card" style="margin-top: 1rem">
        <div class="status-line" style="justify-content: space-between">
          <h2 style="margin: 0">
            Draft
            <span v-if="draft" class="muted">v{{ draft.version }}</span>
          </h2>
          <button
            v-if="draft && !editing"
            type="button"
            data-testid="edit-draft"
            @click="editing = true"
          >
            Edit draft
          </button>
        </div>

        <p v-if="savedMessage" class="notice" role="status">
          {{ savedMessage }}
        </p>
        <p v-if="saveError" class="error-text" role="alert">{{ saveError }}</p>

        <p v-if="!draft" class="muted" data-testid="clarify-no-draft">
          No draft exists yet. Regenerate drafts to create one.
        </p>

        <template v-else-if="!editing">
          <div data-testid="draft-preview">
            <p v-if="draft.subject">
              <strong>Subject:</strong> {{ draft.subject }}
            </p>
            <p v-if="draft.greeting">{{ draft.greeting }}</p>
            <p v-if="draft.intro">{{ draft.intro }}</p>
            <p
              v-for="paragraph in draftParagraphs(draft)"
              :key="paragraph.zite_record_id"
            >
              {{ paragraph.text }}
            </p>
            <p v-if="draft.closing">{{ draft.closing }}</p>
          </div>
          <p class="muted">
            Preview renders the draft fields as text; server HTML is never
            injected.
          </p>
          <details>
            <summary>View raw draft JSON</summary>
            <pre>{{ JSON.stringify(draft, null, 2) }}</pre>
          </details>
        </template>

        <form v-else @submit.prevent="save">
          <p>
            <label>
              Subject
              <input
                v-model="editable.subject"
                type="text"
                name="subject"
                @input="mark('subject')"
              />
            </label>
          </p>
          <p>
            <label>
              Greeting
              <textarea
                v-model="editable.greeting"
                name="greeting"
                rows="2"
                @input="mark('greeting')"
              />
            </label>
          </p>
          <p>
            <label>
              Intro
              <textarea
                v-model="editable.intro"
                name="intro"
                rows="3"
                @input="mark('intro')"
              />
            </label>
          </p>
          <fieldset style="border: 0; padding: 0; margin: 0 0 0.75rem">
            <legend class="muted">Per-child paragraphs</legend>
            <p v-for="id in paragraphOrder" :key="id">
              <label>
                {{ childLabel(id) }}
                <textarea
                  v-model="paragraphText[id]"
                  :name="`paragraph-${id}`"
                  rows="3"
                  @input="mark('paragraphs')"
                />
              </label>
            </p>
          </fieldset>
          <p>
            <label>
              Closing
              <textarea
                v-model="editable.closing"
                name="closing"
                rows="2"
                @input="mark('closing')"
              />
            </label>
          </p>

          <div class="dialog-actions" style="justify-content: flex-start">
            <button type="submit" class="primary" :disabled="saving">
              {{ saving ? 'Saving…' : 'Save draft' }}
            </button>
            <button type="button" @click="reloadDraft">Cancel edit</button>
          </div>
        </form>

        <ConflictPanel
          v-if="conflict"
          :conflict="conflict"
          :draft="flattenLocal()"
          :labels="LABELS"
          @rebase="rebase"
          @reload="reloadDraft"
        />
      </section>
    </template>

    <ConfirmDialog
      :open="regenerateOpen"
      title="Regenerate draft"
      message="Regenerating replaces the current draft with a fresh AI draft for this family immediately. Existing edits are not preserved."
      confirm-label="Regenerate draft"
      :dangerous="true"
      @confirm="regenerate"
      @cancel="regenerateOpen = false"
    />
    <ConfirmDialog
      :open="exportOpen"
      title="Export clarification CSV"
      message="Queues a durable export job. The CSV is downloaded from the job's artifact once it finishes."
      confirm-label="Enqueue export"
      @confirm="exportCsv"
      @cancel="exportOpen = false"
    />
  </section>
</template>
