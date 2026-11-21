<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  auditDuplication,
  decideDuplication,
  getDuplication,
  type DuplicationDecision,
  type DuplicationDetail,
  type DuplicationMember,
} from '../api/duplications'
import { patchEntity, type Entity, type EntityPatch } from '../api/audit'
import { useAuditNav } from '../composables/useAuditNav'
import { useSessionStore } from '../stores/session'
import { useDirtyGuard } from '../composables/useDirtyGuard'
import { ENTITY_FIELDS, formatTime } from '../utils/format'
import { normalizeValue, toAge } from '../utils/diff'
import AuditBackLink from '../components/AuditBackLink.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const route = useRoute()
const session = useSessionStore()
const detail = ref<DuplicationDetail | null>(null)
const draft = reactive<Record<string, unknown>>({})
const touched = ref<Set<string>>(new Set())
/** The decisions the auditor toggled but has not saved yet. */
const pending = ref<Map<string, DuplicationDecision>>(new Map())
const loading = ref(false)
const saving = ref(false)
const loadError = ref<string | null>(null)
const submitError = ref<string | null>(null)
const notice = ref<string | null>(null)
const stale = ref(false)
const ignoreTarget = ref<DuplicationMember | null>(null)

/** Identity fields live in the column header, not in the field rows. */
const IDENTITY_FIELDS = new Set(['zite_record_id', 'zite_created_at', 'created'])
/** Fields the server stores as integers. */
const NUMERIC_FIELDS = new Set(['age_reported', 'num_children'])
/** Entity key column on the wire. */
const KEY_FIELD: Record<string, string> = {
  child: 'zite_record_id',
  family: 'family_id',
  sponsor: 'sponsor_id',
}

const groupId = computed(() => String(route.params.id ?? ''))
const seasonName = computed(() => session.season?.name ?? '')
/** The list's filters, carried in the URL, decide the neighbours. */
const filters = computed(() => {
  const params = new URLSearchParams(carriedQuery.value)
  return { q: params.get('q') ?? '', entity: params.get('entity') }
})

/** The back button returns to the duplications list this group came from. */
const { listUrl, query: carriedQuery } = useAuditNav('/audit/duplications')
const members = computed<DuplicationMember[]>(() => detail.value?.members ?? [])
const entity = computed<Entity>(() =>
  detail.value?.entity === 'family' || detail.value?.entity === 'sponsor'
    ? detail.value.entity
    : 'child',
)
const editableFields = computed(() => new Set(ENTITY_FIELDS[entity.value].map((spec) => spec.field)))
const labels = computed<Record<string, string>>(() =>
  Object.fromEntries(
    ENTITY_FIELDS[entity.value].map((spec) => [spec.field, spec.label]),
  ),
)

/** The group's current screened row: the survivor's row, or the first one. */
const currentMember = computed<DuplicationMember | null>(
  () =>
    members.value.find((member) => member.survivor && member.screened) ??
    members.value.find((member) => member.screened) ??
    null,
)
const currentRow = computed<Record<string, unknown> | null>(
  () => (currentMember.value?.screened ?? null) as Record<string, unknown> | null,
)
const currentKey = computed<string>(() => {
  const row = currentRow.value
  if (!row) return ''
  return String(row[KEY_FIELD[entity.value]] ?? '')
})
const isDirty = computed(() => touched.value.size > 0 || pending.value.size > 0)
/** Every member must be resolved before the group can be audited. */
const canMarkAudited = computed(
  () =>
    detail.value !== null &&
    detail.value.status !== 'audited' &&
    members.value.every((member) => decisionOf(member) !== null) &&
    !isDirty.value,
)
const auditHint = computed(() => {
  if (!detail.value) return 'Resolve every member first.'
  if (detail.value.status === 'audited') {
    return 'The group is audited; auditing again reconciles the final rows again.'
  }
  if (isDirty.value) return 'Save or discard your changes first.'
  return 'Marks the group audited and reconciles the affected final rows.'
})

useDirtyGuard(isDirty)

/** The raw fields the group's submissions disagree or differ on. */
const rawFields = computed<string[]>(() => {
  const fields: string[] = []
  for (const member of members.value) {
    for (const key of Object.keys(member.raw ?? {})) {
      if (IDENTITY_FIELDS.has(key) || fields.includes(key)) continue
      fields.push(key)
    }
  }
  return fields
})

/**
 * Whether the group's submissions conflict on one field.
 *
 * Matching values do not conflict, and neither does a field one
 * submission leaves blank while another fills in.
 */
function conflicts(field: string): boolean {
  const values = new Set<string>()
  for (const member of members.value) {
    const value = member.raw?.[field]
    if (value === null || value === undefined || value === '') continue
    values.add(String(value))
  }
  return values.size > 1
}

function rowClass(field: string): string {
  return conflicts(field) ? 'diff-changed' : ''
}

function labelOf(field: string): string {
  return labels.value[field] ?? field
}

function show(value: unknown): string {
  return normalizeValue(value) || '—'
}

function isEditable(field: string): boolean {
  return editableFields.value.has(field)
}

/** The URL of one neighbouring group, keeping the list's filters. */
function neighbourTo(key: string): string {
  const path = `/audit/duplications/${encodeURIComponent(key)}`
  return carriedQuery.value ? `${path}?${carriedQuery.value}` : path
}

/** The current value differs from the group's raw consensus. */
function currentDiffers(field: string): boolean {
  const row = currentRow.value
  if (!row) return false
  if (conflicts(field)) return false
  const raw = members.value
    .map((member) => member.raw?.[field])
    .filter((value) => value !== null && value !== undefined && value !== '')
  if (raw.length === 0) return normalizeValue(row[field]) !== ''
  return normalizeValue(row[field]) !== String(raw[0])
}

function resetDraft(): void {
  for (const key of Object.keys(draft)) delete draft[key]
  const row = currentRow.value
  if (!row) return
  for (const field of rawFields.value) {
    if (isEditable(field)) draft[field] = row[field] ?? ''
  }
  touched.value = new Set()
}

function setField(field: string, value: unknown): void {
  draft[field] = value
  const next = new Set(touched.value)
  next.add(field)
  touched.value = next
  notice.value = null
}

function payload(): EntityPatch {
  const body: Record<string, unknown> = {}
  for (const field of touched.value) {
    const value = draft[field]
    body[field] = NUMERIC_FIELDS.has(field)
      ? toAge(value)
      : normalizeValue(value)
        ? String(value)
        : null
  }
  return body as EntityPatch
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    loadError.value = 'No active season loaded.'
    return
  }
  if (!groupId.value) {
    loadError.value = 'No duplicate group selected.'
    return
  }
  loading.value = true
  loadError.value = null
  submitError.value = null
  stale.value = false
  notice.value = null
  try {
    detail.value = await getDuplication(
      seasonName.value,
      groupId.value,
      filters.value,
    )
    resetDraft()
  } catch (failure) {
    loadError.value =
      failure instanceof Error ? failure.message : 'detail request failed'
  } finally {
    loading.value = false
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'request failed'
}

/** The decision a member carries: the pending one, else the stored one. */
function decisionOf(member: DuplicationMember): DuplicationDecision | null {
  const chosen = pending.value.get(member.zite_record_id)
  if (chosen) return chosen
  const stored = member.decision
  return stored === 'not_duplicate' || stored === 'ignore' ? stored : null
}

/** Toggle one member's decision; the change is applied when saving. */
function toggleDecision(
  member: DuplicationMember,
  decision: DuplicationDecision,
): void {
  const next = new Map(pending.value)
  if (decisionOf(member) === decision) {
    next.delete(member.zite_record_id)
  } else {
    next.set(member.zite_record_id, decision)
  }
  pending.value = next
  notice.value = null
}

function askIgnore(member: DuplicationMember): void {
  if (decisionOf(member) === 'ignore') return
  ignoreTarget.value = member
}

/** Confirm the pending ignore; it is applied when saving. */
function confirmIgnore(): void {
  const target = ignoreTarget.value
  ignoreTarget.value = null
  if (target) toggleDecision(target, 'ignore')
}

/** Mark the group audited; the server reconciles the affected rows. */
async function markAudited(): Promise<void> {
  if (!detail.value || saving.value) return
  saving.value = true
  submitError.value = null
  stale.value = false
  notice.value = null
  try {
    detail.value = await auditDuplication(
      seasonName.value,
      groupId.value,
      filters.value,
    )
    pending.value = new Map()
    resetDraft()
    notice.value = 'Duplication marked audited.'
  } catch (failure) {
    stale.value = failure instanceof ApiError && failure.status === 412
    submitError.value = messageOf(failure)
  } finally {
    saving.value = false
  }
}

/** Save the edited current values onto the group's screened row. */
async function save(): Promise<void> {
  const member = currentMember.value
  const version = member?.row_version
  if (!member || version === null || version === undefined) return
  if (touched.value.size === 0 && pending.value.size === 0) return
  saving.value = true
  submitError.value = null
  stale.value = false
  notice.value = null
  try {
    // The decisions first: a decision can bump the current row's version.
    for (const [recordId, decision] of pending.value) {
      const target = members.value.find(
        (candidate) => candidate.zite_record_id === recordId,
      )
      if (!target) continue
      detail.value = await decideDuplication(
        seasonName.value,
        groupId.value,
        recordId,
        decision,
        target.row_version,
        filters.value,
      )
    }
    // The edited current values, with the version the decisions left behind.
    if (touched.value.size > 0) {
      const current = currentMember.value
      const currentVersion = current?.row_version
      if (current && currentVersion !== null && currentVersion !== undefined) {
        const updated = await patchEntity(
          seasonName.value,
          entity.value,
          currentKey.value,
          payload(),
          currentVersion,
        )
        current.screened = updated.row as Record<string, unknown>
        current.row_version = updated.row_version
      }
    }
    const decided = pending.value.size
    const edited = touched.value.size
    pending.value = new Map()
    resetDraft()
    notice.value = `Saved ${decided} decision(s) and ${edited} field(s).`
  } catch (failure) {
    stale.value = failure instanceof ApiError && failure.status === 412
    submitError.value = messageOf(failure)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section>
    <p v-if="loading && !detail" role="status">Loading group…</p>
    <p
      v-else-if="loadError"
      class="error-text"
      role="alert"
      data-testid="detail-error"
    >
      {{ loadError }} <button type="button" @click="load">Retry</button>
    </p>

    <template v-else-if="detail">
      <div class="status-line" style="justify-content: space-between">
        <div class="status-line">
          <AuditBackLink :to="listUrl" label="All duplications" />
          <h1 style="margin: 0">Group {{ detail.group_key }}</h1>
          <span class="badge">{{ detail.entity }}</span>
          <StatusBadge :status="detail.status" />
        </div>
        <div class="status-line">
          <button
            type="button"
            :class="detail.status === 'audited' ? '' : 'primary'"
            :disabled="saving || !canMarkAudited"
            :title="auditHint"
            data-testid="mark-audited"
            @click="markAudited"
          >
            {{ detail.status === 'audited' ? 'Re-audit' : 'Mark audited' }}
          </button>
        </div>
      </div>
      <p class="muted">
        {{ members.length }} submission(s). The columns are the raw
        submissions; the last column is the group's current screened row
        and can be edited.
      </p>

      <p
        v-if="submitError"
        class="error-text"
        role="alert"
        data-testid="decision-error"
      >
        {{ submitError }}
        <button v-if="stale" type="button" @click="load">Reload</button>
      </p>
      <p v-if="notice" class="notice" data-testid="decision-notice">
        {{ notice }}
      </p>

      <div style="overflow-x: auto">
        <table class="data editor-table">
          <colgroup>
            <col style="width: 18%" />
            <col
              v-for="member in members"
              :key="member.zite_record_id"
              style="width: 22%"
            />
            <col style="width: 28%" />
          </colgroup>
          <thead>
            <tr>
              <th>Field</th>
              <th
                v-for="member in members"
                :key="member.zite_record_id"
                :data-member="member.zite_record_id"
              >
                <div>{{ member.zite_record_id }}</div>
                <div class="muted">{{ formatTime(member.created) }}</div>
                <span v-if="member.survivor" class="badge">survivor</span>
                <div class="status-line" style="gap: 0.5rem">
                  <label class="muted">
                    <input
                      type="checkbox"
                      data-testid="not-duplicate"
                      :aria-label="`Not duplicate for ${member.zite_record_id}`"
                      :checked="decisionOf(member) === 'not_duplicate'"
                      :disabled="saving"
                      @click.prevent="toggleDecision(member, 'not_duplicate')"
                    />
                    Not duplicate
                  </label>
                  <label class="muted">
                    <input
                      type="checkbox"
                      data-testid="ignore"
                      :aria-label="`Ignore for ${member.zite_record_id}`"
                      :checked="decisionOf(member) === 'ignore'"
                      :disabled="saving"
                      @click.prevent="askIgnore(member)"
                    />
                    Ignore
                  </label>
                </div>
                <span v-if="member.assigned_id" class="muted">
                  → {{ member.assigned_id }}
                </span>
                <span
                  v-if="pending.has(member.zite_record_id)"
                  class="badge"
                  data-testid="pending-decision"
                >
                  unsaved
                </span>
              </th>
              <th>Current (editable)</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="field in rawFields"
              :key="field"
              :class="rowClass(field)"
              :data-field="field"
            >
              <td>
                {{ labelOf(field) }}
                <span v-if="conflicts(field)" class="badge">conflict</span>
                <span
                  v-if="currentDiffers(field)"
                  class="badge"
                  data-testid="diff-marker"
                >
                  differs
                </span>
              </td>
              <td
                v-for="member in members"
                :key="member.zite_record_id"
                :data-member="member.zite_record_id"
              >
                {{ show(member.raw?.[field]) }}
              </td>
              <td>
                <input
                  v-if="isEditable(field)"
                  :name="field"
                  type="text"
                  :value="(draft[field] as string)"
                  :data-field="field"
                  @input="
                    setField(
                      field,
                      ($event.target as HTMLInputElement).value,
                    )
                  "
                />
                <span v-else>{{ show(currentRow?.[field]) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="status-line" style="margin-top: 0.75rem">
        <RouterLink
          v-if="detail.previous"
          class="chip"
          :to="neighbourTo(detail.previous.key)"
          data-testid="previous-entry"
        >
          ← Previous {{ detail.previous.label }}
        </RouterLink>
        <span v-else class="chip muted" data-testid="previous-entry">
          ← Previous
        </span>
        <RouterLink
          v-if="detail.next"
          class="chip"
          :to="neighbourTo(detail.next.key)"
          data-testid="next-entry"
        >
          Next {{ detail.next.label }} →
        </RouterLink>
        <span v-else class="chip muted" data-testid="next-entry">Next →</span>
      </div>

      <div class="status-line" style="margin-top: 0.75rem">
        <button
          type="button"
          class="primary"
          :disabled="saving || !isDirty"
          data-testid="save"
          @click="save"
        >
          Save current values
        </button>
        <button type="button" :disabled="saving || !isDirty" @click="resetDraft">
          Reset local edits
        </button>
        <span v-if="isDirty" class="muted">
          {{ touched.size }} field(s) changed but not saved
        </span>
      </div>

      <p class="muted" style="margin-top: 0.75rem">
        A field conflicts when the submissions disagree on it; a field one
        submission leaves blank while another fills in does not conflict.
        “Not duplicate” gives the submission its own unique id; “Ignore”
        destroys the adjudicated row. Both are recorded against the group
        when you save.
      </p>

      <ConfirmDialog
        :open="ignoreTarget !== null"
        title="Ignore this submission?"
        message="Ignoring destroys this row's adjudicated data — it is reduced to its identity skeleton. Un-ignore later restores the raw baseline values and loses the edits. The decision is applied when you save."
        confirm-label="Ignore on save"
        dangerous
        @confirm="confirmIgnore"
        @cancel="ignoreTarget = null"
      />
    </template>
  </section>
</template>
