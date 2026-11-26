<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError } from '../api/client'
import { getEntity, patchEntity, setIgnored, SEGMENT, type Entity, type EntityDetail, type EntityPatch } from '../api/audit'
import { markManualDuplicate } from '../api/duplications'
import { useSessionStore } from '../stores/session'
import { useAuditNav } from '../composables/useAuditNav'
import { useDirtyGuard } from '../composables/useDirtyGuard'
import { ENTITY_FIELDS, formatTime, type FieldSpec } from '../utils/format'
import { checkClass, checkLabel } from '../utils/status'
import { differs, normalizeValue, toAge } from '../utils/diff'
import { conflictFromError, type ConflictInfo } from '../utils/conflict'
import StatusBadge from './StatusBadge.vue'
import SpecialNeedsEditor from './SpecialNeedsEditor.vue'
import ConflictPanel from './ConflictPanel.vue'
import ConfirmDialog from './ConfirmDialog.vue'

/** `embedded`: shown in a popup on another page, so no previous/next neighbours. */
const props = defineProps<{ entity: Entity; entityKey: string; embedded?: boolean }>()

const session = useSessionStore()
const detail = ref<EntityDetail | null>(null)
const draft = reactive<Record<string, unknown>>({})
const touched = ref<Set<string>>(new Set())
const loading = ref(false)
const saving = ref(false)
const loadError = ref<string | null>(null)
const submitError = ref<string | null>(null)
const notice = ref<string | null>(null)
const conflict = ref<ConflictInfo | null>(null)
const confirmAction = ref<'ignore' | 'unignore' | null>(null)

const NUMERIC_FIELDS = new Set(['age_reported', 'num_children'])
const ENTITY_LABEL: Record<Entity, string> = {
  sponsor: 'Sponsor',
  family: 'Family',
  child: 'Child',
}

const seasonName = computed(() => session.season?.name ?? '')
const fieldSpecs = computed<FieldSpec[]>(() =>
  ENTITY_FIELDS[props.entity].filter((spec) => !spec.hidden),
)
/** The list's filters, carried in the URL, decide the neighbours. */
const { query: carriedQuery } = useAuditNav('/audit')
const listFilters = computed(() => {
  // The host page's query belongs to the host, not to an audit list.
  if (props.embedded) return { q: '', status: null }
  const params = new URLSearchParams(carriedQuery.value)
  return { q: params.get('q') ?? '', status: params.get('status') }
})
const labels = computed<Record<string, string>>(() =>
  Object.fromEntries(
    [...fieldSpecs.value, { field: 'clarification_notes', label: 'Clarification notes' }].map(
      (spec) => [spec.field, spec.label],
    ),
  ),
)
const BOOLEAN_FIELDS = computed(
  () =>
    new Set(
      fieldSpecs.value.filter((spec) => spec.kind === 'boolean').map((spec) => spec.field),
    ),
)
const neutralFields = computed(() =>
  fieldSpecs.value.filter((spec) => spec.neutral).map((spec) => spec.field),
)
const isDirty = computed(() => touched.value.size > 0)
const isIgnored = computed(() => detail.value?.row.ignore === true)
const isAudited = computed(() => detail.value?.status === 'audited')
const isAdmin = computed(() => session.hasRole('admin'))
const auditConfirm = ref(false)
/** Status control sent with the next save (null = plain field save). */
const pendingStatus = ref<'audited' | 'new' | null>(null)

// Manual duplicate marking (issue 5): pair this row with another row of
// the same entity so both join the Duplications queue.
const dupPartner = ref('')
const dupBusy = ref(false)
const dupMessage = ref<string | null>(null)
const dupConfirm = ref(false)

function askMarkDuplicate(): void {
  dupMessage.value = null
  dupConfirm.value = true
}

async function confirmMarkDuplicate(): Promise<void> {
  dupConfirm.value = false
  const partner = dupPartner.value.trim()
  if (!partner) return
  dupBusy.value = true
  try {
    await markManualDuplicate(
      seasonName.value,
      props.entity,
      props.entityKey,
      partner,
    )
    dupMessage.value = `Marked as a duplicate of ${partner} — adjudicate the pair in the Duplications queue.`
    dupPartner.value = ''
  } catch (failure) {
    dupMessage.value =
      failure instanceof Error ? failure.message : 'mark failed'
  } finally {
    dupBusy.value = false
  }
}

const title = computed(
  () => `${ENTITY_LABEL[props.entity]} ${detail.value?.key ?? props.entityKey}`,
)

const isOrphan = computed(
  () =>
    props.entity === 'child' &&
    detail.value !== null &&
    detail.value.row.family_id === null,
)

const duplicatePending = computed(
  () => detail.value?.row.duplicate_pending === true,
)

const duplicateGroupId = computed(() => {
  const id = detail.value?.row.duplicate_group_id
  return id === null || id === undefined || id === '' ? '' : String(id)
})

const childFamilyId = computed(() => {
  const id = detail.value?.row.family_id
  return id === null || id === undefined || id === '' ? '' : String(id)
})

const familyChildCount = computed(() =>
  Number(detail.value?.row.family_child_count ?? 0),
)

/** Message for a row the server refuses to audit until its group is resolved. */
function duplicatePendingMessage(): string {
  const group = duplicateGroupId.value
  return group
    ? `This row belongs to duplicate group ${group}, which still awaits audit. Resolve it in Duplications before marking this row audited.`
    : 'This row belongs to a duplicate group that still awaits audit. Resolve it in Duplications before marking this row audited.'
}

const auditHint = computed(() => {
  if (isAudited.value) {
    return 'This row is audited; its values are final. An admin can undo the audit to change it.'
  }
  if (duplicatePending.value) {
    return 'This row is part of a duplicate group that still awaits audit; resolve the group in Duplications first.'
  }
  if (isOrphan.value) {
    return 'An orphaned child cannot be marked audited until a matching family registration exists.'
  }
  if (isIgnored.value) {
    return 'An ignored row stays ignored; un-ignore it before auditing.'
  }
  return 'Accepts the current values as the final entry.'
})

/** Email/address validation status (families and sponsors only). */
const validation = computed(() => {
  const diagnostics = (detail.value?.diagnostics ?? {}) as Record<
    string,
    unknown
  >
  return {
    email: diagnostics.email_status as string | undefined,
    address: diagnostics.address_status as string | undefined,
  }
})

const ageMismatch = computed<string | null>(() => {
  if (props.entity !== 'child' || !detail.value) return null
  const reported = detail.value.row.age_reported
  const atXmas = detail.value.row.age_at_xmas
  if (reported === null || atXmas === null || reported === undefined) return null
  if (Number(reported) === Number(atXmas)) return null
  return `Age reported (${String(reported)}) differs from age at Christmas (${String(atXmas)}); verify DOB and age.`
})

useDirtyGuard(isDirty)

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'request failed'
}

function show(value: unknown): string {
  return normalizeValue(value) || '—'
}

function resetDraft(): void {
  const current = detail.value
  if (!current) return
  for (const key of Object.keys(draft)) delete draft[key]
  for (const spec of fieldSpecs.value) {
    draft[spec.field] = current.row[spec.field] ?? ''
  }
  draft.clarification_notes = current.row.clarification_notes ?? ''
  touched.value = new Set()
}

function apply(updated: EntityDetail): void {
  detail.value = updated
  resetDraft()
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
    body[field] = BOOLEAN_FIELDS.value.has(field)
      ? draft[field] === true
      : NUMERIC_FIELDS.has(field)
        ? toAge(draft[field])
        : normalizeValue(draft[field])
          ? String(draft[field])
          : null
  }
  if (pendingStatus.value) body.status = pendingStatus.value
  return body as EntityPatch
}

/**
 * Whether the draft differs from the baseline. A checkbox cannot say
 * "unset", so for boolean fields null and false count as the same value.
 */
function fieldDiffers(spec: FieldSpec): boolean {
  const current = draft[spec.field]
  const baseline = detail.value?.baseline[spec.field]
  return spec.kind === 'boolean'
    ? (current === true) !== (baseline === true)
    : differs(current, baseline)
}

/** The baseline cell text; booleans read Yes/No, unset stays a dash. */
function baselineText(spec: FieldSpec): string {
  const value = detail.value?.baseline[spec.field]
  if (spec.kind === 'boolean' && typeof value === 'boolean') {
    return value ? 'Yes' : 'No'
  }
  return show(value)
}

function diffClass(spec: FieldSpec): string {
  if (!detail.value) return ''
  if (!fieldDiffers(spec)) return ''
  return spec.neutral ? 'diff-neutral' : 'diff-changed'
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    loadError.value = 'No active season loaded.'
    return
  }
  loading.value = true
  loadError.value = null
  conflict.value = null
  submitError.value = null
  notice.value = null
  pendingStatus.value = null
  try {
    apply(
      await getEntity(
        seasonName.value,
        props.entity,
        props.entityKey,
        listFilters.value,
      ),
    )
  } catch (failure) {
    loadError.value = messageOf(failure)
  } finally {
    loading.value = false
  }
}

/** The URL of one neighbouring entry, keeping the list's filters. */
function neighbourTo(key: string): string {
  const path = `/audit/${SEGMENT[props.entity]}/${encodeURIComponent(key)}`
  return carriedQuery.value ? `${path}?${carriedQuery.value}` : path
}

async function persist(version: number): Promise<void> {
  if (!detail.value || (touched.value.size === 0 && !pendingStatus.value))
    return
  saving.value = true
  submitError.value = null
  try {
    apply(
      await patchEntity(
        seasonName.value,
        props.entity,
        props.entityKey,
        payload(),
        version,
      ),
    )
    notice.value =
      pendingStatus.value === 'audited'
        ? 'Marked audited — the current values are the final entry.'
        : pendingStatus.value === 'new'
          ? 'Audit undone.'
          : 'Saved.'
    pendingStatus.value = null
  } catch (failure) {
    const next =
      failure instanceof ApiError
        ? conflictFromError(failure, 'edit')
        : null
    if (next) conflict.value = next
    else if (failure instanceof ApiError && failure.code === 'duplicate_pending')
      submitError.value = duplicatePendingMessage()
    else if (failure instanceof ApiError && failure.code === 'row_audited')
      submitError.value =
        'This row is audited; its values are final. An admin can undo the audit first.'
    else if (
      failure instanceof ApiError &&
      (failure.code === 'admin_required' || failure.status === 403)
    )
      submitError.value = 'Only an admin may undo an audit.'
    else submitError.value = messageOf(failure)
  } finally {
    saving.value = false
  }
}

function save(): void {
  if (detail.value) void persist(detail.value.row_version)
}

/** Mark the row audited (or undo it), asking what to do with unsaved edits. */
function toggleAudited(): void {
  if (!detail.value) return
  if (isAudited.value) {
    pendingStatus.value = 'new'
    void persist(detail.value.row_version)
    return
  }
  if (isDirty.value) {
    auditConfirm.value = true
    return
  }
  pendingStatus.value = 'audited'
  void persist(detail.value.row_version)
}

function confirmAuditSave(): void {
  auditConfirm.value = false
  if (!detail.value) return
  pendingStatus.value = 'audited'
  void persist(detail.value.row_version)
}

function confirmAuditDiscard(): void {
  auditConfirm.value = false
  if (!detail.value) return
  resetDraft()
  pendingStatus.value = 'audited'
  void persist(detail.value.row_version)
}

function reloadConflict(): void {
  conflict.value = null
  void load()
}

/** Keep the user's touched values, apply them to the fresh row, resubmit. */
async function rebase(): Promise<void> {
  const info = conflict.value
  const current = detail.value
  if (!info || !current) return
  for (const spec of fieldSpecs.value) {
    if (!touched.value.has(spec.field)) {
      draft[spec.field] = info.row[spec.field] ?? ''
    }
  }
  if (!touched.value.has('clarification_notes')) {
    draft.clarification_notes = info.row.clarification_notes ?? ''
  }
  detail.value = {
    ...current,
    row: info.row,
    row_version: info.currentVersion,
    last_updated_by: info.lastUpdatedBy,
    last_updated_at: info.lastUpdatedAt,
  }
  conflict.value = null
  await persist(info.currentVersion)
}

function askIgnore(): void {
  confirmAction.value = isIgnored.value ? 'unignore' : 'ignore'
}

async function confirmIgnore(): Promise<void> {
  const action = confirmAction.value
  const current = detail.value
  confirmAction.value = null
  if (!action || !current) return
  saving.value = true
  submitError.value = null
  try {
    apply(
      await setIgnored(
        seasonName.value,
        props.entity,
        props.entityKey,
        action,
        current.row_version,
      ),
    )
    notice.value = action === 'ignore' ? 'Row ignored.' : 'Row un-ignored.'
  } catch (failure) {
    const next =
      failure instanceof ApiError
        ? conflictFromError(failure, 'ignore')
        : null
    if (next) conflict.value = next
    else submitError.value = messageOf(failure)
  } finally {
    saving.value = false
  }
}

watch(() => [props.entity, props.entityKey], () => void load())
onMounted(() => void load())
</script>

<template>
  <section data-testid="entity-editor">
    <p v-if="loading && !detail" role="status">Loading {{ entity }}…</p>
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
          <slot name="back" />
          <h1 style="margin: 0">{{ title }}</h1>
          <StatusBadge :status="detail.status" />
          <span class="muted">row version {{ detail.row_version }}</span>
        </div>
        <div class="status-line">
          <button
            type="button"
            :disabled="saving || !isDirty || isAudited"
            @click="save"
          >
            Save changes
          </button>
          <button
            v-if="!isAudited || isAdmin"
            type="button"
            :class="isAudited ? '' : 'primary'"
            :disabled="
              saving || (!isAudited && (isIgnored || isOrphan || duplicatePending))
            "
            :title="auditHint"
            data-testid="mark-audited"
            @click="toggleAudited"
          >
            {{ isAudited ? 'Undo audit' : 'Mark audited' }}
          </button>
        </div>
      </div>
      <p class="muted">
        Last updated by
        <strong>{{ detail.last_updated_by ?? 'never' }}</strong>
        at {{ formatTime(detail.last_updated_at) }} · audited at
        {{ formatTime(detail.audited_at) }}
      </p>
      <p class="muted">“Mark audited” {{ auditHint }}</p>

      <p v-if="isOrphan" class="card" data-testid="orphan-warning">
        <strong>Orphaned child:</strong> no matching family registration.
        Audited status stays disabled (legacy behavior); fix the family link
        before auditing.
      </p>
      <p
        v-if="duplicatePending"
        class="card"
        data-testid="duplicate-pending-warning"
      >
        <strong>Duplicate pending audit:</strong> this row belongs to a
        duplicate group that has not been audited yet, so it cannot be marked
        audited.
        <RouterLink
          v-if="duplicateGroupId"
          :to="`/audit/duplications/${duplicateGroupId}`"
        >
          Resolve it in Duplications
        </RouterLink>
      </p>
      <p v-if="isAudited" class="card" data-testid="audited-lock">
        <strong>Audited:</strong> this row is audited and its values are final.
        <template v-if="isAdmin">
          Undo the audit to change it, then mark it audited again.
        </template>
        <template v-else>Only an admin can undo the audit to change it.</template>
      </p>
      <p v-if="entity === 'child'" class="card" data-testid="child-identity">
        <strong>Angel ID:</strong> {{ show(detail.row.angel_id) }} ·
        <strong>Children in family:</strong> {{ familyChildCount }}
        <template v-if="childFamilyId">
          ·
          <RouterLink :to="`/audit/families/${childFamilyId}`">
            Family {{ childFamilyId }}
          </RouterLink>
        </template>
      </p>
      <p v-if="ageMismatch" class="card diff-changed" data-testid="age-warning">
        <strong>Age mismatch:</strong> {{ ageMismatch }}
      </p>
      <p
        v-if="entity !== 'child'"
        class="card"
        data-testid="validation-status"
      >
        <strong>Validation:</strong>
        <span class="badge" :class="checkClass(validation.email)">
          email: {{ checkLabel(validation.email) }}
        </span>
        <span
          v-if="entity === 'family'"
          class="badge"
          :class="checkClass(validation.address)"
        >
          address: {{ checkLabel(validation.address) }}
        </span>
      </p>

      <ConflictPanel
        v-if="conflict"
        :conflict="conflict"
        :draft="draft"
        :labels="labels"
        :neutral-fields="neutralFields"
        @reload="reloadConflict"
        @rebase="rebase"
      />

      <p
        v-if="submitError"
        class="error-text"
        role="alert"
        data-testid="save-error"
      >
        {{ submitError }}
      </p>
      <p v-if="notice" class="notice" data-testid="save-notice">{{ notice }}</p>

      <p
        v-if="entity === 'sponsor' && !embedded"
        class="muted"
        data-testid="manual-matching-pointer"
      >
        Place families by hand in
        <RouterLink to="/match">Match</RouterLink>.
      </p>

      <h2>Baseline vs current</h2>
      <p class="muted">
        Prepared baseline on the left, editable current values on the right.
        Only touched fields are sent; an empty value clears the field.
      </p>
      <div style="overflow-x: auto">
        <table class="data editor-table">
          <colgroup>
            <col style="width: 18%" />
            <col style="width: 32%" />
            <col style="width: 50%" />
          </colgroup>
          <thead>
            <tr>
              <th>Field</th>
              <th>Prepared baseline</th>
              <th>Current (editable)</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="spec in fieldSpecs"
              :key="spec.field"
              :class="diffClass(spec)"
              :data-field="spec.field"
            >
              <td>
                {{ spec.label }}
                <span v-if="spec.neutral" class="badge">control</span>
                <span
                  v-if="fieldDiffers(spec)"
                  class="badge"
                  data-testid="diff-marker"
                >
                  differs{{ spec.neutral ? ' (not status-relevant)' : '' }}
                </span>
              </td>
              <td>{{ baselineText(spec) }}</td>
              <td>
                <SpecialNeedsEditor
                  v-if="spec.field === 'special_needs'"
                  :model-value="(draft.special_needs as string | null)"
                  :disabled="isAudited"
                  @update:model-value="setField('special_needs', $event)"
                />
                <input
                  v-else-if="spec.kind === 'boolean'"
                  :name="spec.field"
                  type="checkbox"
                  :aria-label="spec.label"
                  :checked="draft[spec.field] === true"
                  :disabled="isAudited"
                  @change="
                    setField(spec.field, ($event.target as HTMLInputElement).checked)
                  "
                />
                <textarea
                  v-else-if="spec.kind === 'textarea'"
                  :name="spec.field"
                  rows="3"
                  :value="(draft[spec.field] as string)"
                  :disabled="isAudited"
                  @input="
                    setField(
                      spec.field,
                      ($event.target as HTMLTextAreaElement).value,
                    )
                  "
                />
                <input
                  v-else
                  :name="spec.field"
                  :type="spec.kind === 'number' ? 'number' : spec.kind === 'date' ? 'date' : 'text'"
                  :value="(draft[spec.field] as string)"
                  :disabled="isAudited"
                  @input="
                    setField(spec.field, ($event.target as HTMLInputElement).value)
                  "
                />
              </td>
            </tr>
            <tr :data-field="'clarification_notes'">
              <td>Clarification notes</td>
              <td>{{ show(detail.baseline.clarification_notes) }}</td>
              <td>
                <textarea
                  name="clarification_notes"
                  rows="4"
                  :value="(draft.clarification_notes as string)"
                  :disabled="isAudited"
                  @input="
                    setField(
                      'clarification_notes',
                      ($event.target as HTMLTextAreaElement).value,
                    )
                  "
                />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="status-line" style="margin-top: 0.75rem">
        <button
          type="button"
          class="primary"
          :disabled="saving || !isDirty || isAudited"
          data-testid="save"
          @click="save"
        >
          Save
        </button>
        <button
          type="button"
          :disabled="saving || !isDirty || isAudited"
          @click="resetDraft"
        >
          Reset local edits
        </button>
        <span v-if="isDirty" class="muted">
          {{ touched.size }} field(s) changed but not saved
        </span>
      </div>

      <div v-if="!embedded" class="status-line" style="margin-top: 0.75rem">
        <RouterLink
          v-if="detail.previous"
          class="chip"
          :to="neighbourTo(detail.previous.key)"
          data-testid="previous-entry"
        >
          ← Previous
          {{ detail.previous.label }}
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

      <section class="card" style="margin-top: 1rem">
        <h2 style="margin-top: 0">Ignore controls</h2>
        <p class="muted">
          Ignoring destroys this row's adjudicated data — it is reduced to its
          identity skeleton. Un-ignore later restores the raw baseline values
          and loses the edits. Saves use row version
          {{ detail.row_version }}; a stale version never overwrites silently.
        </p>
        <button
          type="button"
          :class="isIgnored ? '' : 'danger'"
          :disabled="saving || isAudited"
          data-testid="ignore-toggle"
          @click="askIgnore"
        >
          {{
            isIgnored
              ? 'Un-ignore (restores raw values)'
              : 'Ignore (destructive)'
          }}
        </button>
      </section>

      <section class="card" style="margin-top: 1rem">
        <h2 style="margin-top: 0">Mark as duplicate</h2>
        <p class="muted">
          Suspect this {{ entity }} is the same person as another row the
          screening missed? Enter that row's key (its id on its own audit
          page) to queue the pair in the Duplications section, where the
          two are merged or separated deliberately.
        </p>
        <div class="filters">
          <input
            v-model="dupPartner"
            type="text"
            name="partner"
            :placeholder="`Other ${entity} key`"
            aria-label="Duplicate partner key"
            data-testid="dup-partner"
          />
          <button
            type="button"
            :disabled="dupBusy || !dupPartner.trim()"
            data-testid="mark-duplicate"
            @click="askMarkDuplicate"
          >
            Mark as duplicate
          </button>
        </div>
        <p v-if="dupMessage" class="muted" data-testid="dup-message">
          {{ dupMessage }}
        </p>
      </section>

      <ConfirmDialog
        :open="dupConfirm"
        title="Mark as duplicate?"
        :message="`Queue this ${entity} and ${dupPartner.trim() || 'the other row'} as one duplicate pair for the Duplications audit? Nothing is merged until you decide there.`"
        confirm-label="Queue the pair"
        @confirm="confirmMarkDuplicate"
        @cancel="dupConfirm = false"
      />

      <ConfirmDialog
        :open="confirmAction !== null"
        :title="confirmAction === 'ignore' ? 'Ignore this row?' : 'Un-ignore this row?'"
        :message="
          confirmAction === 'ignore'
            ? 'Ignoring destroys this row\'s adjudicated data — it is reduced to its identity skeleton. Un-ignore later restores the raw baseline values and loses the edits.'
            : 'Un-ignoring re-seeds the data columns from the raw baseline — previous adjudicated edits to this row are lost.'
        "
        :confirm-label="
          confirmAction === 'ignore' ? 'Ignore and save' : 'Un-ignore and save'
        "
        :dangerous="confirmAction === 'ignore'"
        @confirm="confirmIgnore"
        @cancel="confirmAction = null"
      />

      <ConfirmDialog
        :open="auditConfirm"
        title="Unsaved changes"
        message="This row has unsaved changes. Save them and mark the row audited, or mark the stored values audited without them."
        confirm-label="Save and mark audited"
        alternate-label="Mark audited without saving"
        @confirm="confirmAuditSave"
        @alternate="confirmAuditDiscard"
        @cancel="auditConfirm = false"
      />
    </template>
  </section>
</template>
