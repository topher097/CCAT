<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { listEntities, type Entity } from '../api/audit'
import { useSessionStore } from '../stores/session'
import { STATUSES } from '../utils/status'
import { formatTime } from '../utils/format'
import { loadChildCounts } from '../utils/childCounts'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{
  segment: 'sponsors' | 'families' | 'children'
}>()

const session = useSessionStore()

/** URL segment -> API entity name. */
const ENTITY: Record<'sponsors' | 'families' | 'children', Entity> = {
  sponsors: 'sponsor',
  families: 'family',
  children: 'child',
}

/** Entity key column on the wire. */
const KEY_FIELD: Record<Entity, string> = {
  sponsor: 'sponsor_id',
  family: 'family_id',
  child: 'zite_record_id',
}

const entity = computed(() => ENTITY[props.segment])
const rows = ref<Record<string, unknown>[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const query = ref('')
const status = ref<string>('all')
const loading = ref(false)
const error = ref<string | null>(null)
const childCounts = ref<Map<string, number>>(new Map())
let countsLoaded = false

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
)
const seasonName = computed(() => session.season?.name ?? '')

function keyOf(row: Record<string, unknown>): string {
  return String(row[KEY_FIELD[entity.value]] ?? '')
}

function displayName(row: Record<string, unknown>): string {
  const name = [row.first_name, row.last_name]
    .filter((part) => part !== null && part !== undefined && part !== '')
    .join(' ')
  return (
    name || String(row.primary_email ?? row.email ?? row[KEY_FIELD[entity.value]])
  )
}

function emailOf(row: Record<string, unknown>): string {
  return String(row.primary_email ?? row.email ?? '')
}

function detailTo(row: Record<string, unknown>): string {
  if (entity.value === 'sponsor') return `/audit/sponsors/${keyOf(row)}`
  if (entity.value === 'family') return `/audit/families/${keyOf(row)}`
  return `/audit/children/${keyOf(row)}`
}

function isOrphan(row: Record<string, unknown>): boolean {
  return entity.value === 'child' && row.family_id === null
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    error.value = 'No active season loaded.'
    return
  }
  loading.value = true
  error.value = null
  try {
    const result = await listEntities(seasonName.value, entity.value, {
      page: page.value,
      page_size: pageSize.value,
      q: query.value,
      status: status.value,
    })
    rows.value = result.items as Record<string, unknown>[]
    total.value = result.total
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'list request failed'
  } finally {
    loading.value = false
  }
}

async function ensureChildCounts(): Promise<void> {
  if (countsLoaded || entity.value !== 'family') return
  countsLoaded = true
  try {
    childCounts.value = await loadChildCounts(seasonName.value)
  } catch {
    countsLoaded = false // retry on the next load
  }
}

function submitSearch(): void {
  page.value = 1
  void load()
}

function setStatus(next: string): void {
  status.value = next
  page.value = 1
  void load()
}

function go(next: number): void {
  page.value = Math.min(Math.max(1, next), totalPages.value)
  void load()
}

watch(
  () => props.segment,
  () => {
    page.value = 1
    query.value = ''
    status.value = 'all'
    countsLoaded = false
    childCounts.value = new Map()
    void load()
  },
)

onMounted(() => {
  void load()
  void ensureChildCounts()
})
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1 style="text-transform: capitalize">{{ props.segment }}</h1>
      <RouterLink to="/audit">Audit landing</RouterLink>
    </div>

    <form class="filters" @submit.prevent="submitSearch">
      <input
        v-model="query"
        type="search"
        name="q"
        placeholder="Search name, email, or id"
        aria-label="Search"
      />
      <button type="submit">Search</button>
      <span class="muted" data-testid="list-total">{{ total }} rows</span>
    </form>

    <div class="filters" role="group" aria-label="Status filter">
      <button
        type="button"
        class="chip"
        :aria-pressed="status === 'all'"
        @click="setStatus('all')"
      >
        all
      </button>
      <button
        v-for="option in STATUSES"
        :key="option"
        type="button"
        class="chip"
        :aria-pressed="status === option"
        @click="setStatus(option)"
      >
        {{ option.replaceAll('_', ' ') }}
      </button>
    </div>

    <p v-if="loading" role="status">Loading {{ props.segment }}…</p>
    <p v-else-if="error" class="error-text" role="alert" data-testid="list-error">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p v-else-if="rows.length === 0" class="muted" data-testid="list-empty">
      No matching {{ props.segment }}.
    </p>

    <table v-else class="data">
      <thead>
        <tr>
          <th>Status</th>
          <th>Key</th>
          <th>Name</th>
          <th>Email</th>
          <th v-if="entity === 'child'">Family</th>
          <th v-if="entity === 'family'">Children</th>
          <th>Indicators</th>
          <th>Last updated</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="keyOf(row)">
          <td><StatusBadge :status="String(row.status ?? 'new')" /></td>
          <td>
            <RouterLink :to="detailTo(row)">{{ keyOf(row) }}</RouterLink>
          </td>
          <td>{{ displayName(row) }}</td>
          <td>{{ emailOf(row) }}</td>
          <td v-if="entity === 'child'">
            <RouterLink
              v-if="row.family_id !== null"
              :to="`/audit/families/${row.family_id}`"
            >
              {{ row.family_id }}
            </RouterLink>
            <span v-else class="badge">orphan</span>
          </td>
          <td v-if="entity === 'family'">
            {{ childCounts.get(keyOf(row)) ?? 0 }}
          </td>
          <td>
            <span v-if="row.ignore === true" class="badge">ignored</span>
            <span v-if="isOrphan(row)" class="badge">orphan</span>
            <span v-if="row.potential_duplicate === true" class="badge">
              possible duplicate
            </span>
            <span v-if="row.raw_missing === true" class="badge">
              raw missing
            </span>
          </td>
          <td class="muted">
            {{ row.last_updated_by ?? '—' }}<br />
            {{ formatTime(row.last_updated_at as string | undefined) }}
          </td>
        </tr>
      </tbody>
    </table>

    <div class="pagination">
      <button type="button" :disabled="page <= 1" @click="go(page - 1)">
        Previous
      </button>
      <span data-testid="list-page">
        Page {{ page }} of {{ totalPages }} ({{ total }} total)
      </span>
      <button
        type="button"
        :disabled="page >= totalPages"
        @click="go(page + 1)"
      >
        Next
      </button>
      <label class="muted">
        Page size
        <select v-model.number="pageSize" @change="submitSearch">
          <option :value="25">25</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
    </div>

    <p class="muted" style="margin-top: 0.75rem">
      Ignored, orphaned and raw-missing rows stay visible. Raw-missing rows are
      not flagged by the list API yet (no field); open the detail to compare
      against the prepared baseline.
    </p>
  </section>
</template>
