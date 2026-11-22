<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  assignDuplicationChildren,
  auditDuplication,
  decideDuplication,
  getDuplication,
  type DuplicationDecision,
  type DuplicationDetail,
  type DuplicationMember,
} from '../api/duplications'
import { patchEntity, SEGMENT, type Entity, type EntityPatch } from '../api/audit'
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
/**
 * The row key the Current column is bound to. It follows the surviving entry
 * while the column is clean, and stays fixed while the column holds edits.
 */
const boundKey = ref<string | null>(null)
/** Per-child family choice for a split family group. */
const childrenDraft = reactive<Record<string, number | null>>({})
const loading = ref(false)
const saving = ref(false)
const loadError = ref<string | null>(null)
const submitError = ref<string | null>(null)
const notice = ref<string | null>(null)
const stale = ref(false)
const ignoreTarget = ref<DuplicationMember | null>(null)
const auditConfirm = ref(false)
const blockerOpen = ref(false)

/** Identity fields live in the column header, not in the field rows. */
const IDENTITY_FIELDS = new Set(['zite_record_id', 'zite_created_at', 'created'])
/** Fields the server stores as integers. */
const NUMERIC_FIELDS = new Set(['age_reported', 'num_children'])

const groupId = computed(() => String(route.params.id ?? ''))
const seasonName = computed(() => session.season?.name ?? '')
/** The back button returns to the duplications list this group came from. */
const { listUrl, query: carriedQuery } = useAuditNav('/audit/duplications')
/** The list's filters, carried in the URL, decide the neighbours. */
const filters = computed(() => {
  const params = new URLSearchParams(carriedQuery.value)
  return { q: params.get('q') ?? '', entity: params.get('entity') }
})
const members = computed<DuplicationMember[]>(
  () => (detail.value?.members ?? []) as DuplicationMember[],
)
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

/** The flagged survivor from the screening run — a suggestion, not the authority. */
const flaggedMember = computed<DuplicationMember | null>(
  () =>
    members.value.find((member) => member.survivor && member.screened) ??
    members.value.find((member) => member.survivor) ??
    null,
)
/** Members with no effective decision are candidates to survive. */
const undecidedMembers = computed<DuplicationMember[]>(() =>
  members.value.filter((member) => decisionOf(member) === null),
)
/** The single surviving entry, when the group is resolved. */
const survivingEntry = computed<DuplicationMember | null>(() => {
  if (undecidedMembers.value.length !== 1) return null
  return undecidedMembers.value[0] ?? null
})
/** The member the Current column would bind to if it were clean. */
const preferredMember = computed<DuplicationMember | null>(
  () => survivingEntry.value ?? flaggedMember.value,
)
/** The member the Current column is actually bound to. */
const boundMember = computed<DuplicationMember | null>(() => {
  if (touched.value.size > 0 && boundKey.value) {
    return (
      members.value.find((member) => member.row_key === boundKey.value) ?? null
    )
  }
  return preferredMember.value
})
/** The column is locked to a different row than the current surviving entry. */
const bindingLocked = computed(
  () =>
    boundMember.value !== null &&
    boundMember.value.row_key !== preferredMember.value?.row_key,
)
const currentRow = computed<Record<string, unknown> | null>(
  () => (boundMember.value?.screened ?? null) as Record<string, unknown> | null,
)
const currentKey = computed<string>(() => boundMember.value?.row_key ?? '')
const currentReadonly = computed(
  () => boundMember.value?.row_status === 'audited',
)
const isDirty = computed(() => touched.value.size > 0 || pending.value.size > 0)
const blockerMessage = computed(() => {
  const count = undecidedMembers.value.length
  if (count === 0)
    return 'No submission is left to survive — discard or release them all.'
  return `${count} submissions are still undecided — this group has more than one surviving entry.`
})
const auditHint = computed(() => {
  if (!detail.value) return ''
  if (undecidedMembers.value.length !== 1) return blockerMessage.value
  if (isDirty.value)
    return 'Marks the group audited; you choose whether to save the unsaved changes first.'
  if (detail.value.status === 'audited')
    return 'The group is audited; auditing again reconciles the final rows again.'
  return 'Marks the group audited and reconciles the affected final rows.'
})
const auditConfirmMessage = computed(() => {
  const fields = touched.value.size
  const decisions = pending.value.size
  return `This group has unsaved changes: ${fields} field(s) in the current column and ${decisions} member decision(s). The member decisions are saved either way; the current column's edits are kept only by 'Save and mark audited'.`
})
const currentBindingText = computed(() => {
  if (!boundMember.value) return ''
  if (bindingLocked.value) return ''
  if (
    undecidedMembers.value.length === 1 &&
    boundMember.value.row_key === survivingEntry.value?.row_key
  ) {
    return `Current column: the surviving entry, ${boundMember.value.zite_record_id}.`
  }
  return `Current column: provisional — the submission the screening run kept, ${boundMember.value.zite_record_id}.`
})
const lockedText = computed(() => {
  if (!boundMember.value) return ''
  return `The current column still holds unsaved edits for ${boundMember.value.zite_record_id}. Save or reset them before it moves to another row.`
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

const entityHint = computed(() => {
  if (entity.value === 'child') {
    return 'Not duplicate gives this submission its own angel letter in the same family: it becomes a second angel of that family, with its own sheet and its own sponsor assignment. Ignore destroys this row.'
  }
  if (entity.value === 'sponsor') {
    return 'Not duplicate only clears the duplicate flag; this submission already owns its own sponsor id. Ignore destroys this row.'
  }
  return "This group's submissions share one family row. Ignore drops this registration and keeps the family — destroy the family from the family's own row. Not duplicate creates a new family, and the group's children need a decision about which family they belong to."
})

/** Family choices for each child of a split family group. */
const familyOptions = computed(() => {
  const options: { label: string; value: number }[] = []
  const survivor = survivingEntry.value
  if (survivor) {
    const id = Number(survivor.row_key)
    if (!Number.isNaN(id))
      options.push({ label: `Surviving family (#${id})`, value: id })
  }
  for (const member of members.value) {
    if (effectiveState(member) !== 'released') continue
    const idStr = member.assigned_id ?? member.row_key
    const id = Number(idStr)
    if (!Number.isNaN(id))
      options.push({ label: `Released family (#${id})`, value: id })
  }
  return options
})

const childrenChanged = computed(() => {
  if (!detail.value) return false
  return detail.value.children.some((child) => {
    const chosen = childrenDraft[child.zite_record_id]
    return chosen !== undefined && chosen !== child.family_id
  })
})

/** Map each child's zite record id to the member record id it was submitted with. */
const childSource = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const member of members.value) {
    for (const child of member.children) {
      map[child.zite_record_id] = member.zite_record_id
    }
  }
  return map
})

function submittedWith(childId: string): string {
  return childSource.value[childId] ?? '—'
}

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
  touched.value = new Set()
  boundKey.value = preferredMember.value?.row_key ?? null
  const row = boundMember.value?.screened ?? null
  if (!row) return
  for (const field of rawFields.value) {
    if (isEditable(field)) draft[field] = row[field] ?? ''
  }
}

function resetChildrenDraft(): void {
  for (const key of Object.keys(childrenDraft)) delete childrenDraft[key]
  for (const child of detail.value?.children ?? []) {
    childrenDraft[child.zite_record_id] = child.family_id ?? null
  }
}

function setField(field: string, value: unknown): void {
  if (currentReadonly.value) return
  if (!boundKey.value) boundKey.value = boundMember.value?.row_key ?? null
  draft[field] = value
  const next = new Set(touched.value)
  next.add(field)
  touched.value = next
  notice.value = null
}

function payload(): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  for (const field of touched.value) {
    const value = draft[field]
    body[field] = NUMERIC_FIELDS.has(field)
      ? toAge(value)
      : normalizeValue(value)
        ? String(value)
        : null
  }
  return body
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
  pending.value = new Map()
  try {
    detail.value = await getDuplication(
      seasonName.value,
      groupId.value,
      filters.value,
    )
    resetDraft()
    resetChildrenDraft()
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

/** The effective state of a member from its effective decision. */
function effectiveState(
  member: DuplicationMember,
): 'surviving' | 'released' | 'discarded' {
  const decision = decisionOf(member)
  if (decision === 'not_duplicate') return 'released'
  if (decision === 'ignore') return 'discarded'
  return 'surviving'
}

/** Human label for the member's effective state. */
function stateLabel(member: DuplicationMember): string {
  const state = effectiveState(member)
  if (state === 'surviving') return 'surviving entry'
  if (state === 'released') return 'released — its own entry now'
  return 'discarded'
}

/** Link to a released member's own row page. */
function rowUrl(member: DuplicationMember): string {
  return `/audit/${SEGMENT[entity.value]}/${encodeURIComponent(member.row_key)}`
}

/** Text for the released-member link. */
function rowLinkText(member: DuplicationMember): string {
  if (member.row_status === 'audited')
    return 'Open its own row — already audited'
  return 'Open its own row — still needs its own audit'
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

/** Ask what to do with unsaved changes, then mark the group audited. */
async function markAudited(): Promise<void> {
  if (!detail.value || saving.value) return
  if (undecidedMembers.value.length !== 1) {
    blockerOpen.value = true
    return
  }
  if (isDirty.value) {
    auditConfirm.value = true
    return
  }
  await performAudit()
}

/** Post every pending member decision and update the local detail. */
async function postPendingDecisions(): Promise<void> {
  if (!detail.value) return
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
  pending.value = new Map()
}

/** Mark the group audited with an optional values payload. */
async function performAudit(values?: Record<string, unknown>): Promise<void> {
  if (!detail.value || saving.value) return
  saving.value = true
  submitError.value = null
  stale.value = false
  notice.value = null
  try {
    const bound = boundMember.value
    const version = bound?.row_version
    const body: Record<string, unknown> = {}
    if (values !== undefined && Object.keys(values).length > 0)
      body.values = values
    if (version !== null && version !== undefined)
      body.expected_version = version
    detail.value = await auditDuplication(
      seasonName.value,
      groupId.value,
      filters.value,
      body,
    )
    pending.value = new Map()
    resetDraft()
    resetChildrenDraft()
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
  const member = boundMember.value
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
      const current = boundMember.value
      const currentVersion = current?.row_version
      if (current && currentVersion !== null && currentVersion !== undefined) {
        const updated = await patchEntity(
          seasonName.value,
          entity.value,
          currentKey.value,
          payload() as EntityPatch,
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

/** Save the pending edits, then audit the group with the current values. */
async function confirmAuditSave(): Promise<void> {
  auditConfirm.value = false
  await postPendingDecisions()
  if (submitError.value) return
  const values = payload()
  await performAudit(values)
}

/** Drop local edits and member decisions, then audit the stored values. */
async function confirmAuditDiscard(): Promise<void> {
  auditConfirm.value = false
  await postPendingDecisions()
  if (submitError.value) return
  resetDraft()
  await performAudit()
}

function setChildFamily(recordId: string, familyId: string): void {
  const id = familyId === '' ? null : Number(familyId)
  childrenDraft[recordId] = Number.isNaN(id) ? null : id
}

async function saveChildren(): Promise<void> {
  if (!detail.value || saving.value) return
  const assignments: Record<string, number> = {}
  for (const child of detail.value.children) {
    const chosen = childrenDraft[child.zite_record_id]
    if (
      chosen !== undefined &&
      chosen !== null &&
      chosen !== child.family_id
    ) {
      assignments[child.zite_record_id] = chosen
    }
  }
  if (Object.keys(assignments).length === 0) return
  saving.value = true
  submitError.value = null
  notice.value = null
  try {
    detail.value = await assignDuplicationChildren(
      seasonName.value,
      groupId.value,
      assignments,
      filters.value,
    )
    resetChildrenDraft()
    notice.value = 'Family assignments saved.'
  } catch (failure) {
    submitError.value = messageOf(failure)
  } finally {
    saving.value = false
  }
}

watch(groupId, () => void load())
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
            :disabled="saving"
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
                <div data-testid="member-state">{{ stateLabel(member) }}</div>
                <span
                  v-if="member.survivor"
                  class="badge"
                  data-testid="suggested-survivor"
                  :data-member="member.zite_record_id"
                >
                  suggested survivor
                </span>
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
                <div v-if="effectiveState(member) === 'released'">
                  <RouterLink
                    :to="rowUrl(member)"
                    data-testid="released-link"
                  >
                    {{ rowLinkText(member) }}
                  </RouterLink>
                </div>
                <div
                  v-if="member.children.length > 0"
                  data-testid="member-children"
                >
                  <div class="muted">Children submitted with this entry</div>
                  <ul>
                    <li
                      v-for="child in member.children"
                      :key="child.zite_record_id"
                    >
                      {{ child.first_name ?? '—' }} · {{ child.dob ?? '—' }}
                    </li>
                  </ul>
                </div>
              </th>
              <th>
                <div>Current (editable)</div>
                <div v-if="boundMember" data-testid="current-binding">
                  {{ currentBindingText }}
                </div>
                <div
                  v-if="bindingLocked"
                  class="muted"
                  data-testid="binding-locked"
                >
                  {{ lockedText }}
                </div>
                <div
                  v-if="currentReadonly"
                  class="muted"
                  data-testid="current-readonly"
                >
                  This row is audited — its values are final.
                </div>
              </th>
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
                  :readonly="currentReadonly"
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

      <p class="muted" style="margin-top: 0.75rem">{{ entityHint }}</p>

      <div
        v-if="detail.children.length > 0"
        class="card"
        style="margin-top: 1rem"
        data-testid="group-children"
      >
        <h3 style="margin-top: 0">Children in this group</h3>
        <p class="muted">
          These children are matched to each entry by submission time as a hint,
          not a decision — the auditor still assigns them below.
        </p>
        <p class="muted">Children left alone stay with the surviving family.</p>
        <table class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Date of birth</th>
              <th>Submitted with</th>
              <th>Current family</th>
              <th>Assign to</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="child in detail.children"
              :key="child.zite_record_id"
              :data-child="child.zite_record_id"
            >
              <td>{{ child.first_name ?? '—' }}</td>
              <td>{{ child.dob ?? '—' }}</td>
              <td>{{ submittedWith(child.zite_record_id) }}</td>
              <td>{{ child.family_id ?? '—' }}</td>
              <td>
                <select
                  :value="childrenDraft[child.zite_record_id] ?? ''"
                  data-testid="child-family-select"
                  @change="
                    setChildFamily(
                      child.zite_record_id,
                      ($event.target as HTMLSelectElement).value,
                    )
                  "
                >
                  <option value="">—</option>
                  <option
                    v-for="option in familyOptions"
                    :key="option.value"
                    :value="option.value"
                  >
                    {{ option.label }}
                  </option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="status-line" style="margin-top: 0.75rem">
          <button
            type="button"
            class="primary"
            :disabled="saving || !childrenChanged"
            data-testid="save-children"
            @click="saveChildren"
          >
            Save the family assignments
          </button>
        </div>
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

      <ConfirmDialog
        :open="auditConfirm"
        title="Unsaved changes"
        :message="auditConfirmMessage"
        confirm-label="Save and mark audited"
        alternate-label="Mark audited without saving"
        @confirm="confirmAuditSave"
        @alternate="confirmAuditDiscard"
        @cancel="auditConfirm = false"
      />

      <ConfirmDialog
        :open="blockerOpen"
        title="This group cannot be audited yet"
        :message="blockerMessage"
        confirm-label="Close"
        @confirm="blockerOpen = false"
        @cancel="blockerOpen = false"
      />
    </template>
  </section>
</template>
