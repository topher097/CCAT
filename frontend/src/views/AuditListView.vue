<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { listEntities, type Entity } from '../api/audit'
import { useAuditNav } from '../composables/useAuditNav'
import { useSessionStore } from '../stores/session'
import { STATUSES, checkClass, checkLabel } from '../utils/status'
import { formatTime } from '../utils/format'
import AuditBackLink from '../components/AuditBackLink.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{
  segment: 'sponsors' | 'families' | 'children'
}>()

const route = useRoute()
const router = useRouter()
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

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
)
const seasonName = computed(() => session.season?.name ?? '')
/** The filters travel in the URL, so a detail link carries them. */
const { query: carriedQuery } = useAuditNav(() => `/audit/${props.segment}`)

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
  const path =
    entity.value === 'sponsor'
      ? `/audit/sponsors/${keyOf(row)}`
      : entity.value === 'family'
        ? `/audit/families/${keyOf(row)}`
        : `/audit/children/${keyOf(row)}`
  return carriedQuery.value ? `${path}?${carriedQuery.value}` : path
}

function isOrphan(row: Record<string, unknown>): boolean {
  return entity.value === 'child' && row.family_id === null
}

/** Read the list's filters and page back out of the URL. */
function readUrl(): void {
  query.value = String(route.query.q ?? '')
  status.value = String(route.query.status ?? 'all')
  page.value = Math.max(1, Number(route.query.page ?? 1) || 1)
  pageSize.value = Math.max(1, Number(route.query.page_size ?? 50) || 50)
}

/** Write the list's filters and page into the URL. */
function writeUrl(): void {
  const next: Record<string, string> = {}
  if (query.value) next.q = query.value
  if (status.value !== 'all') next.status = status.value
  if (page.value > 1) next.page = String(page.value)
  if (pageSize.value !== 50) next.page_size = String(pageSize.value)
  void router.replace({ path: route.path, query: next })
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

function submitSearch(): void {
  page.value = 1
  writeUrl()
}

function setStatus(next: string): void {
  status.value = next
  page.value = 1
  writeUrl()
}

function go(next: number): void {
  page.value = Math.min(Math.max(1, next), totalPages.value)
  writeUrl()
}

watch(
  () => route.fullPath,
  () => {
    readUrl()
    void load()
  },
)

onMounted(() => {
  readUrl()
  void load()
})
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <div class="status-line">
        <AuditBackLink to="/audit" label="Audit" />
        <h1 style="text-transform: capitalize">{{ props.segment }}</h1>
      </div>
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
          <th v-if="entity !== 'child'">Validation</th>
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
            {{ Number(row.child_count ?? 0) }}
          </td>
          <td v-if="entity !== 'child'" data-testid="validation">
            <span class="badge" :class="checkClass(row.email_status)">
              email: {{ checkLabel(row.email_status) }}
            </span>
            <span
              v-if="entity === 'family'"
              class="badge"
              :class="checkClass(row.address_status)"
            >
              address: {{ checkLabel(row.address_status) }}
            </span>
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
      Ignored, orphaned and raw-missing rows stay visible. A “raw missing”
      badge marks a row whose source record is absent from the current raw snapshot.
    </p>
  </section>
</template>
