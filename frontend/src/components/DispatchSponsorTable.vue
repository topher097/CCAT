<script setup lang="ts">
/** Filterable sponsor packet work list; all data stays in the parent view. */

import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import type { DispatchRow } from '../api/dispatch'
import { formatTime } from '../utils/format'
import {
  dispatchBlock,
  organization,
  personName,
  remainingOf,
} from '../utils/sponsor'

const props = withDefaults(
  defineProps<{
    rows: DispatchRow[]
    canSend: boolean
    mailConfigured?: boolean
  }>(),
  { mailConfigured: true },
)

const emit = defineEmits<{
  openEmail: [row: DispatchRow]
  generatePacket: [sponsorId: number, overwrite: boolean]
}>()

type AssignmentFilter = 'any' | 'filled' | 'unfilled'
type SortKey = 'readiness' | 'id' | 'name' | 'assigned'
type ChipKey =
  | 'needs-action'
  | 'ready'
  | 'missing-sheets'
  | 'missing-packet'
  | 'missing-email'
  | 'failed'
  | 'dispatched'

const CHIPS: { key: ChipKey; label: string }[] = [
  { key: 'needs-action', label: 'Needs action' },
  { key: 'ready', label: 'Ready to send' },
  { key: 'missing-sheets', label: 'Missing sheets' },
  { key: 'missing-packet', label: 'Missing packet' },
  { key: 'missing-email', label: 'Missing email' },
  { key: 'failed', label: 'Send failed' },
  { key: 'dispatched', label: 'Dispatched' },
]

const BLOCKERS: Record<string, string> = {
  no_email: 'No email on file',
  missing_sheets: 'Child sheets missing',
  slots_not_filled: 'Child slots not filled',
  manifest_missing: 'Packet manifest missing',
  already_sent: 'Already sent',
  packet_changed: 'Packet changed since the draft was created',
  mail_not_configured: 'Mail account not configured',
}

const search = ref('')
const affiliation = ref('')
const assignment = ref<AssignmentFilter>('any')
const sort = ref<SortKey>('readiness')
const chips = ref<ChipKey[]>(['needs-action'])

const affiliations = computed(() =>
  [
    ...new Set(
      props.rows
        .map((row) => row.sponsor_type)
        .filter((value): value is string => Boolean(value)),
    ),
  ].sort((a, b) => a.localeCompare(b)),
)

const allSelected = computed(() => chips.value.length === 0)
function blockerLabel(code: string): string {
  return BLOCKERS[code] ?? code.replaceAll('_', ' ')
}

function readyToSend(row: DispatchRow): boolean {
  return (
    !row.packets_dispatched &&
    row.email_status !== 'sent' &&
    row.packet_status === 'ready' &&
    Boolean(row.email) &&
    row.blockers.length === 0 &&
    dispatchBlock(row) === ''
  )
}

function matchesChip(row: DispatchRow, chip: ChipKey): boolean {
  switch (chip) {
    case 'needs-action':
      return !row.packets_dispatched && row.email_status !== 'sent'
    case 'ready':
      return readyToSend(row)
    case 'missing-sheets':
      return row.missing_sheet_angel_ids.length > 0
    case 'missing-packet':
      return row.packet_status !== 'ready'
    case 'missing-email':
      return !row.email
    case 'failed':
      return row.email_status === 'failed'
    case 'dispatched':
      return row.packets_dispatched
  }
}

function matches(row: DispatchRow): boolean {
  const needle = search.value.trim().toLowerCase()
  if (needle) {
    const haystack = [
      row.sponsor_id,
      `#${row.sponsor_id}`,
      personName(row),
      row.display_name,
      organization(row),
      row.email,
      ...row.assigned_angel_ids,
      ...row.missing_sheet_angel_ids,
    ]
      .filter(Boolean)
      .join('\n')
      .toLowerCase()
    if (!haystack.includes(needle)) return false
  }
  if (affiliation.value && row.sponsor_type !== affiliation.value) return false
  const remaining = remainingOf(row)
  if (assignment.value === 'filled' && remaining !== 0) return false
  if (assignment.value === 'unfilled' && remaining === 0) return false
  return chips.value.every((chip) => matchesChip(row, chip))
}

function readinessRank(row: DispatchRow): number {
  if (readyToSend(row)) return 0
  if (row.email_status === 'failed') return 1
  if (!row.packets_dispatched && row.packet_status === 'ready') return 2
  if (!row.packets_dispatched) return 3
  return 4
}

function compare(a: DispatchRow, b: DispatchRow): number {
  const byId = a.sponsor_id - b.sponsor_id
  switch (sort.value) {
    case 'name':
      return personName(a).localeCompare(personName(b)) || byId
    case 'assigned':
      return b.assigned_count - a.assigned_count || byId
    case 'readiness':
      return readinessRank(a) - readinessRank(b) || byId
    default:
      return byId
  }
}

const visible = computed(() => props.rows.filter(matches).sort(compare))

function toggleChip(chip: ChipKey): void {
  chips.value = chips.value.includes(chip)
    ? chips.value.filter((value) => value !== chip)
    : [...chips.value, chip]
}

function showAll(): void {
  chips.value = []
}

function clearFilters(): void {
  search.value = ''
  affiliation.value = ''
  assignment.value = 'any'
  sort.value = 'readiness'
  chips.value = ['needs-action']
}

function emailLabel(row: DispatchRow): string {
  switch (row.email_status) {
    case 'draft_ready':
      return 'Draft ready'
    case 'draft_edited':
      return 'Draft edited'
    case 'sending':
      return 'Sending…'
    case 'sent':
      return `Sent ${formatTime(row.email_sent_at)}`
    case 'failed':
      return 'Send failed'
    case 'unknown':
      return 'Send outcome unknown'
    default:
      return 'Not drafted'
  }
}

function emailClass(row: DispatchRow): string {
  if (row.email_status === 'sent') return 'status-audited'
  if (row.email_status === 'failed') return 'status-ignored'
  if (['draft_ready', 'draft_edited', 'sending', 'unknown'].includes(row.email_status)) {
    return 'status-in-progress'
  }
  return 'status-new'
}
</script>

<template>
  <section aria-label="Sponsor dispatch work list">
    <div class="filters">
      <input
        v-model="search"
        type="search"
        aria-label="Search dispatch sponsors"
        placeholder="Search sponsor #, name, organization, email, angel ID"
      />
      <label>
        Affiliation
        <select v-model="affiliation" aria-label="Dispatch affiliation filter">
          <option value="">Any affiliation</option>
          <option v-for="value in affiliations" :key="value" :value="value">
            {{ value }}
          </option>
        </select>
      </label>
      <label>
        Assignment
        <select v-model="assignment" aria-label="Dispatch assignment filter">
          <option value="any">Any assignment</option>
          <option value="filled">All slots filled</option>
          <option value="unfilled">Slots not filled</option>
        </select>
      </label>
      <label>
        Sort
        <select v-model="sort" aria-label="Sort dispatch sponsors">
          <option value="readiness">Readiness (ready to send first)</option>
          <option value="id">Sponsor #</option>
          <option value="name">Name A–Z</option>
          <option value="assigned">Assigned children</option>
        </select>
      </label>
    </div>

    <div class="status-line" aria-label="Dispatch filters">
      <button
        v-for="chip in CHIPS"
        :key="chip.key"
        type="button"
        class="chip"
        :aria-pressed="chips.includes(chip.key)"
        @click="toggleChip(chip.key)"
      >
        {{ chip.label }}
      </button>
      <button
        type="button"
        class="chip"
        :aria-pressed="allSelected"
        @click="showAll"
      >
        All sponsors
      </button>
    </div>

    <div class="status-line" style="justify-content: space-between; margin: 0.65rem 0">
      <span class="muted" role="status" data-testid="dispatch-count">
        Showing {{ visible.length }} of {{ props.rows.length }} sponsors
      </span>
      <button type="button" data-testid="clear-filters" @click="clearFilters">
        Clear filters
      </button>
    </div>

    <p v-if="visible.length === 0" class="muted" data-testid="dispatch-no-match">
      No sponsors match these filters.
      <button type="button" @click="clearFilters">Clear filters</button>
    </p>

    <table v-else class="data dispatch-table" data-testid="dispatch-table">
      <thead>
        <tr>
          <th>Sponsor</th>
          <th>Recipient</th>
          <th>Assignment</th>
          <th>Child sheets</th>
          <th>Packet</th>
          <th>Email &amp; dispatch</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in visible"
          :key="row.sponsor_id"
          data-testid="dispatch-row"
          :data-sponsor-id="row.sponsor_id"
        >
          <td>
            <strong>#{{ row.sponsor_id }} {{ personName(row) }}</strong>
            <div v-if="organization(row)" class="muted">{{ organization(row) }}</div>
            <div v-if="row.sponsor_type" class="muted">{{ row.sponsor_type }}</div>
            <RouterLink :to="`/audit/sponsors/${row.sponsor_id}`" class="muted">
              Audit sponsor
            </RouterLink>
          </td>
          <td>
            <span v-if="row.email">{{ row.email }}</span>
            <span v-else class="badge check-bad">No email on file</span>
            <div v-if="row.physical_packet" style="margin-top: 0.3rem">
              <span class="badge check-unchecked">physical packet</span>
            </div>
          </td>
          <td>
            {{ row.assigned_count }} / {{ row.num_children ?? '—' }}
            <div class="muted">{{ row.assigned_angel_ids.join(', ') || 'No angel IDs' }}</div>
          </td>
          <td>
            <span
              class="badge"
              :class="row.missing_sheet_angel_ids.length ? 'check-warn' : 'check-ok'"
            >
              {{ row.sheets_ready_count }} / {{ row.sheets_total }} ready
            </span>
            <div v-if="row.missing_sheet_angel_ids.length" class="muted">
              Missing: {{ row.missing_sheet_angel_ids.join(', ') }}
            </div>
          </td>
          <td>
            <span
              class="badge"
              :class="row.packet_status === 'ready' ? 'check-ok' : 'check-bad'"
            >
              {{ row.packet_status === 'ready' ? 'Ready' : 'Missing' }}
            </span>
          </td>
          <td>
            <span class="badge" :class="emailClass(row)" data-testid="email-status">
              {{ emailLabel(row) }}
            </span>
            <div v-if="row.email_error" class="error-text">{{ row.email_error }}</div>
            <div v-if="row.packets_dispatched" class="notice">
              Dispatched {{ formatTime(row.packets_dispatched_at) }}
            </div>
            <ul
              v-if="row.blockers.length"
              class="error-text blockers"
              data-testid="dispatch-blockers"
            >
              <li v-for="blocker in row.blockers" :key="blocker">
                {{ blockerLabel(blocker) }}
              </li>
            </ul>
          </td>
          <td>
            <div class="actions">
              <button
                v-if="row.packet_status !== 'ready'"
                type="button"
                data-testid="generate-packet"
                @click="emit('generatePacket', row.sponsor_id, false)"
              >
                Generate packet
              </button>
              <button
                v-else
                type="button"
                class="danger"
                data-testid="regenerate-packet"
                @click="emit('generatePacket', row.sponsor_id, true)"
              >
                Regenerate
              </button>
              <button
                v-if="row.email_status === 'sent'"
                type="button"
                data-testid="view-email"
                @click="emit('openEmail', row)"
              >
                View email
              </button>
              <button
                v-else-if="props.canSend && props.mailConfigured"
                type="button"
                class="primary"
                data-testid="draft-send"
                @click="emit('openEmail', row)"
              >
                {{ row.email_status === 'failed' ? 'Retry send…' : 'Draft & send…' }}
              </button>
              <span v-else-if="!props.mailConfigured" class="muted">
                Mail account is not configured.
              </span>
              <span v-else class="muted">Sending requires an administrator.</span>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.dispatch-table {
  min-width: 74rem;
  margin-top: 0.75rem;
}

.actions {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.35rem;
}

.blockers {
  margin: 0.35rem 0 0;
  padding-left: 1.1rem;
  font-size: 0.8rem;
}
</style>
