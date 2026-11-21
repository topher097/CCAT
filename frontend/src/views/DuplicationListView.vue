<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import {
  listDuplications,
  type DuplicationListItem,
} from '../api/duplications'
import { useAuditNav } from '../composables/useAuditNav'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import AuditBackLink from '../components/AuditBackLink.vue'
import StatusBadge from '../components/StatusBadge.vue'

const ENTITIES = ['all', 'child', 'family', 'sponsor']

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const rows = ref<DuplicationListItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const query = ref('')
const entity = ref<string>('all')
const loading = ref(false)
const error = ref<string | null>(null)

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
)
const seasonName = computed(() => session.season?.name ?? '')
/** The filters travel in the URL, so a detail link carries them. */
const { query: carriedQuery } = useAuditNav('/audit/duplications')

function conflicts(row: DuplicationListItem): string {
  const columns = row.conflict_columns ?? []
  return columns.length ? columns.join(', ') : '—'
}

function survivorText(row: DuplicationListItem): string {
  const count = row.survivor_count ?? 0
  if (count === 0) return 'no survivor'
  if (count === 1) return '1 survivor'
  return `${count} survivors`
}

function releasedText(row: DuplicationListItem): string {
  const count = row.released_count ?? 0
  if (count === 1) return '1 released member still needs its own audit'
  return `${count} released members still need their own audit`
}

function detailTo(row: DuplicationListItem): string {
  const path = `/audit/duplications/${row.id}`
  return carriedQuery.value ? `${path}?${carriedQuery.value}` : path
}

/** Read the list's filters and page back out of the URL. */
function readUrl(): void {
  query.value = String(route.query.q ?? '')
  entity.value = String(route.query.entity ?? 'all')
  page.value = Math.max(1, Number(route.query.page ?? 1) || 1)
  pageSize.value = Math.max(1, Number(route.query.page_size ?? 50) || 50)
}

/** Write the list's filters and page into the URL. */
function writeUrl(): void {
  const next: Record<string, string> = {}
  if (query.value) next.q = query.value
  if (entity.value !== 'all') next.entity = entity.value
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
    const result = await listDuplications(seasonName.value, {
      page: page.value,
      page_size: pageSize.value,
      q: query.value,
      entity: entity.value,
    })
    rows.value = (result.items ?? []) as DuplicationListItem[]
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

function setEntity(next: string): void {
  entity.value = next
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
        <h1>Duplications</h1>
      </div>
    </div>

    <form class="filters" @submit.prevent="submitSearch">
      <input
        v-model="query"
        type="search"
        name="q"
        placeholder="Search group key or member id"
        aria-label="Search"
      />
      <button type="submit">Search</button>
      <span class="muted" data-testid="list-total">{{ total }} rows</span>
    </form>

    <div class="filters" role="group" aria-label="Entity filter">
      <button
        v-for="option in ENTITIES"
        :key="option"
        type="button"
        class="chip"
        :aria-pressed="entity === option"
        @click="setEntity(option)"
      >
        {{ option }}
      </button>
    </div>

    <p v-if="loading" role="status">Loading duplications…</p>
    <p v-else-if="error" class="error-text" role="alert" data-testid="list-error">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p v-else-if="rows.length === 0" class="muted" data-testid="list-empty">
      No duplicate groups detected.
    </p>

    <table v-else class="data">
      <thead>
        <tr>
          <th>Group</th>
          <th>Entity</th>
          <th>Members</th>
          <th>Decided</th>
          <th>Survivors</th>
          <th>Conflicts</th>
          <th>Status</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.id">
          <td>
            <RouterLink :to="detailTo(row)">
              {{ row.group_key }}
            </RouterLink>
          </td>
          <td>{{ row.entity }}</td>
          <td>{{ row.member_count }}</td>
          <td>{{ row.decided_count }}</td>
          <td data-testid="survivor-count">{{ survivorText(row) }}</td>
          <td>{{ conflicts(row) }}</td>
          <td>
            <StatusBadge :status="row.status" />
            <span
              v-if="row.released_count > 0"
              class="muted"
              data-testid="released-marker"
            >
              {{ releasedText(row) }}
            </span>
          </td>
          <td class="muted">{{ formatTime(row.created) }}</td>
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
  </section>
</template>
