<script setup lang="ts">
/** /match: run menus, match report, and assignment results (§7.1.10). */

import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError } from '../api/client'
import { listEntities } from '../api/audit'
import { getMatchReport, type MatchReportRow } from '../api/commands'
import {
  listHolds,
  listPool,
  type ManualHoldResult,
  type ManualMatchFamilyCard,
} from '../api/manual-match'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import RunMenu from '../components/RunMenu.vue'

const session = useSessionStore()

const report = ref<MatchReportRow[]>([])
const reportMissing = ref(false)
const reportError = ref<string | null>(null)
const families = ref<Record<string, unknown>[]>([])
const sponsors = ref<Record<string, unknown>[]>([])
const splitFamilies = ref<ManualMatchFamilyCard[]>([])
const holds = ref<ManualHoldResult[]>([])
const assignmentError = ref<string | null>(null)
const manualError = ref<string | null>(null)
const loading = ref(false)

const seasonName = computed(() => session.season?.name ?? '')

const assignedFamilies = computed(() =>
  families.value.filter(
    (row) =>
      row.assigned_sponsor_id !== null && row.assigned_sponsor_id !== undefined,
  ),
)

function angelText(row: Record<string, unknown>): string {
  const value = row.assigned_angel_ids
  if (Array.isArray(value)) return value.join(', ')
  return String(value ?? '').trim()
}

const assignedSponsors = computed(() =>
  sponsors.value.filter((row) => angelText(row) !== ''),
)

async function loadReport(): Promise<void> {
  if (!seasonName.value) return
  reportError.value = null
  reportMissing.value = false
  try {
    report.value = await getMatchReport(seasonName.value)
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 404) {
      reportMissing.value = true
      return
    }
    reportError.value =
      failure instanceof Error ? failure.message : 'match report request failed'
  }
}

async function loadAssignments(): Promise<void> {
  if (!seasonName.value) return
  assignmentError.value = null
  try {
    const [familyPage, sponsorPage] = await Promise.all([
      listEntities(seasonName.value, 'family', { page_size: 200 }),
      listEntities(seasonName.value, 'sponsor', { page_size: 200 }),
    ])
    families.value = familyPage.items as Record<string, unknown>[]
    sponsors.value = sponsorPage.items as Record<string, unknown>[]
  } catch (failure) {
    assignmentError.value =
      failure instanceof Error ? failure.message : 'assignment request failed'
  }
}

async function loadManual(): Promise<void> {
  if (!seasonName.value) return
  manualError.value = null
  try {
    const [splitPage, holdPage] = await Promise.all([
      listPool(seasonName.value, { assignment: 'split', page_size: 50 }),
      listHolds(seasonName.value),
    ])
    splitFamilies.value = splitPage.items ?? []
    holds.value = holdPage.items ?? []
  } catch (failure) {
    manualError.value =
      failure instanceof Error ? failure.message : 'manual matching request failed'
  }
}

async function load(): Promise<void> {
  loading.value = true
  await Promise.all([loadReport(), loadAssignments(), loadManual()])
  loading.value = false
}

onMounted(() => void load())
</script>

<template>
  <section>
    <h1 style="margin-top: 0">Match</h1>
    <p class="muted">
      Runs enqueue a durable job; open it for live progress.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>

    <div class="status-line" style="margin-bottom: 0.75rem">
      <RunMenu command="match" />
      <RunMenu command="generate" />
    </div>

    <section class="card" style="margin-top: 1rem">
      <div class="status-line" style="justify-content: space-between">
        <h2 style="margin: 0">Match report</h2>
        <button type="button" :disabled="loading" @click="load">Refresh</button>
      </div>
      <p v-if="loading" role="status">Loading match data…</p>
      <p v-else-if="reportError" class="error-text" role="alert">
        {{ reportError }}
      </p>
      <p
        v-else-if="reportMissing"
        class="muted"
        data-testid="match-report-empty"
      >
        No match report yet. Enqueue a match run to create one.
      </p>
      <p v-else-if="report.length === 0" class="muted">
        The latest match run wrote no report rows.
      </p>
      <table v-else class="data" data-testid="match-report">
        <thead>
          <tr>
            <th>Kind</th>
            <th>Ref</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in report" :key="index">
            <td>{{ row.kind ?? '—' }}</td>
            <td>{{ row.ref ?? '—' }}</td>
            <td>{{ row.reason ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <p v-if="assignmentError" class="error-text" role="alert">
        {{ assignmentError }}
      </p>
      <template v-else>
        <h2>Assigned sponsors</h2>
        <p v-if="assignedSponsors.length === 0" class="muted">
          No sponsor has assigned angels yet.
        </p>
        <table v-else class="data" data-testid="assigned-sponsors">
          <thead>
            <tr>
              <th>Sponsor</th>
              <th>Name</th>
              <th>Angels</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in assignedSponsors" :key="String(row.sponsor_id)">
              <td>
                <RouterLink :to="`/audit/sponsors/${row.sponsor_id}`">
                  {{ row.sponsor_id }}
                </RouterLink>
              </td>
              <td>
                {{ [row.first_name, row.last_name].filter(Boolean).join(' ') }}
              </td>
              <td>{{ angelText(row) }}</td>
            </tr>
          </tbody>
        </table>

        <h2>Assigned families</h2>
        <p v-if="assignedFamilies.length === 0" class="muted">
          No family has an assigned sponsor yet.
        </p>
        <table v-else class="data" data-testid="assigned-families">
          <thead>
            <tr>
              <th>Family</th>
              <th>Contact</th>
              <th>Sponsor</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in assignedFamilies" :key="String(row.family_id)">
              <td>
                <RouterLink :to="`/audit/families/${row.family_id}`">
                  {{ row.family_id }}
                </RouterLink>
              </td>
              <td>
                {{ [row.first_name, row.last_name].filter(Boolean).join(' ') }}
              </td>
              <td>
                <RouterLink :to="`/audit/sponsors/${row.assigned_sponsor_id}`">
                  {{ row.assigned_sponsor_id }}
                </RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </template>

      <p v-if="manualError" class="error-text" role="alert">
        {{ manualError }}
      </p>
      <template v-else>
        <h2>Split families</h2>
        <p
          v-if="splitFamilies.length === 0"
          class="muted"
          data-testid="split-families-empty"
        >
          No family's children hold more than one sponsor.
        </p>
        <table v-else class="data" data-testid="split-families">
          <thead>
            <tr>
              <th>Family</th>
              <th>Contact</th>
              <th>Sponsors</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in splitFamilies" :key="row.family_id">
              <td>
                <RouterLink :to="`/audit/families/${row.family_id}`">
                  {{ row.family_id }}
                </RouterLink>
              </td>
              <td>
                {{ [row.first_name, row.last_name].filter(Boolean).join(' ') }}
              </td>
              <td>{{ (row.assigned_sponsor_ids ?? []).join(', ') }}</td>
            </tr>
          </tbody>
        </table>

        <h2>Held families</h2>
        <p
          v-if="holds.length === 0"
          class="muted"
          data-testid="held-families-empty"
        >
          No family is held.
        </p>
        <table v-else class="data" data-testid="held-families">
          <thead>
            <tr>
              <th>Family</th>
              <th>Note</th>
              <th>Author</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="hold in holds" :key="hold.family_id">
              <td>
                <RouterLink :to="`/audit/families/${hold.family_id}`">
                  {{ hold.family_id }}
                </RouterLink>
              </td>
              <td>{{ hold.note ?? '—' }}</td>
              <td>{{ hold.created_by ?? '—' }}</td>
              <td class="muted">{{ formatTime(hold.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>
  </section>
</template>
