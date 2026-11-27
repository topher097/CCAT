<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useOverviewStore } from '../stores/overview'
import { useSessionStore } from '../stores/session'
import { STATUSES } from '../utils/status'
import { statusLabel } from '../utils/format'
import { SEGMENT, searchRegistry, type Entity } from '../api/audit'
import AuditBackLink from '../components/AuditBackLink.vue'
import AuditNavCard from '../components/AuditNavCard.vue'
import PipelineRunForm from '../components/PipelineRunForm.vue'
import StatusBadge from '../components/StatusBadge.vue'

/** The duplicate group's own status vocabulary. */
const DUPLICATION_STATUSES = ['pending', 'resolved', 'audited']

type EntityCounts = {
  total?: number
  by_status?: Record<string, number>
  ignored?: number
  orphans?: number
  raw_missing?: number
  pending_ai?: number
  generated?: number
  assigned?: number
}

/** One card per audit section, in the order the sections are audited. */
const SECTIONS = [
  { key: 'child', label: 'Children', to: '/audit/children', statuses: STATUSES },
  { key: 'family', label: 'Families', to: '/audit/families', statuses: STATUSES },
  { key: 'sponsor', label: 'Sponsors', to: '/audit/sponsors', statuses: STATUSES },
  {
    key: 'duplication',
    label: 'Duplications',
    to: '/audit/duplications',
    statuses: DUPLICATION_STATUSES,
  },
]

const session = useSessionStore()
const overview = useOverviewStore()

const seasonName = computed(() => session.season?.name ?? '')
/** The acquire and screen runs replace the old dedicated pages. */
const canRunPipeline = computed(() => session.hasRole('coordinator', 'admin'))
const counts = computed(
  () =>
    (overview.data?.entities ?? {}) as Record<string, EntityCounts | undefined>,
)

/** The footnote under a card: the same extras the overview page lists. */
function noteFor(key: string, entry: EntityCounts | undefined): string {
  if (!entry) return ''
  const byStatus = entry.by_status ?? {}
  if (key === 'duplication') {
    return [
      `pending ${byStatus.pending ?? 0}`,
      `resolved ${byStatus.resolved ?? 0}`,
      `audited ${byStatus.audited ?? 0}`,
    ].join(' · ')
  }
  const parts = [
    `ignored ${entry.ignored ?? 0}`,
    `orphans ${entry.orphans ?? 0}`,
    `raw missing ${entry.raw_missing ?? 0}`,
  ]
  if (key === 'child') {
    parts.push(
      `pending AI ${entry.pending_ai ?? 0}`,
      `sheets generated ${entry.generated ?? 0}`,
    )
  } else {
    parts.push(`assigned ${entry.assigned ?? 0}`)
  }
  return parts.join(' · ')
}

const cards = computed(() =>
  SECTIONS.map((section) => {
    const entry = counts.value[section.key]
    const byStatus = entry?.by_status ?? {}
    return {
      label: section.label,
      to: section.to,
      total: entry?.total ?? 0,
      done: byStatus.audited ?? 0,
      doneLabel: 'audited',
      statuses: section.statuses.map((key) => ({
        key,
        label: statusLabel(key),
        count: byStatus[key] ?? 0,
      })),
      note: noteFor(section.key, entry),
    }
  }),
)

onMounted(async () => {
  if (!session.season) await session.loadSeason()
  await overview.refresh(seasonName.value)
})

/** Cross-registry search: one query over every final_* table. */
type SearchRow = Record<string, unknown>
const ENTITY_LABEL: Record<string, string> = {
  family: 'Family',
  child: 'Child',
  sponsor: 'Sponsor',
}
const route = useRoute()
const router = useRouter()
const query = ref('')
const entityScope = ref<string>('any')
const searchStatus = ref('all')
const searchLocation = ref('any')
const sizeMin = ref('')
const sizeMax = ref('')
const searchIgnore = ref('any')
const searchSponsored = ref('any')
const searchPage = ref(1)
const SEARCH_PAGE_SIZE = 50
const searchRows = ref<SearchRow[]>([])
const searchTotal = ref(0)
const searchLoading = ref(false)
const searchError = ref('')
const searched = ref(false)
const searchPages = computed(() =>
  Math.max(1, Math.ceil(searchTotal.value / SEARCH_PAGE_SIZE)),
)

function triState(value: string): boolean | undefined {
  return value === 'any' ? undefined : value === 'yes'
}

/** Read the search filters back out of the URL. */
function readUrl(): void {
  query.value = String(route.query.q ?? '')
  const scope = String(route.query.entity ?? 'any')
  entityScope.value = ['any', 'family', 'child', 'sponsor'].includes(scope)
    ? scope
    : 'any'
  searchStatus.value = String(route.query.status ?? 'all')
  searchLocation.value = String(route.query.location ?? 'any')
  sizeMin.value = String(route.query.size_min ?? '')
  sizeMax.value = String(route.query.size_max ?? '')
  searchIgnore.value = String(route.query.ignore ?? 'any')
  searchSponsored.value = String(route.query.sponsored ?? 'any')
  searchPage.value = Math.max(1, Number(route.query.page ?? 1) || 1)
}

/** Write the search filters into the URL. */
function writeUrl(): void {
  const next: Record<string, string> = {}
  if (query.value) next.q = query.value
  if (entityScope.value !== 'any') next.entity = entityScope.value
  if (searchStatus.value !== 'all') next.status = searchStatus.value
  if (searchLocation.value !== 'any') next.location = searchLocation.value
  if (sizeMin.value) next.size_min = sizeMin.value
  if (sizeMax.value) next.size_max = sizeMax.value
  if (searchIgnore.value !== 'any') next.ignore = searchIgnore.value
  if (searchSponsored.value !== 'any') next.sponsored = searchSponsored.value
  if (searchPage.value > 1) next.page = String(searchPage.value)
  void router.replace({ path: route.path, query: next })
}

/** A search only fires once the user asked for one. */
function hasFilters(): boolean {
  return (
    query.value !== '' ||
    entityScope.value !== 'any' ||
    searchStatus.value !== 'all' ||
    searchLocation.value !== 'any' ||
    sizeMin.value !== '' ||
    sizeMax.value !== '' ||
    searchIgnore.value !== 'any' ||
    searchSponsored.value !== 'any'
  )
}

async function loadSearch(): Promise<void> {
  if (!seasonName.value) return
  searchLoading.value = true
  searchError.value = ''
  try {
    const result = await searchRegistry(seasonName.value, {
      page: searchPage.value,
      page_size: SEARCH_PAGE_SIZE,
      q: query.value,
      entity: entityScope.value as 'any' | Entity,
      status: searchStatus.value,
      ignore: triState(searchIgnore.value),
      sponsored: triState(searchSponsored.value),
      location: searchLocation.value,
      family_size_min: sizeMin.value === '' ? null : Number(sizeMin.value),
      family_size_max: sizeMax.value === '' ? null : Number(sizeMax.value),
    })
    searchRows.value = (result.items ?? []) as SearchRow[]
    searchTotal.value = result.total
    searched.value = true
  } catch (failure) {
    searchError.value =
      failure instanceof Error ? failure.message : 'search request failed'
  } finally {
    searchLoading.value = false
  }
}

function applySearch(): void {
  searchPage.value = 1
  writeUrl()
}

function searchGo(next: number): void {
  searchPage.value = Math.min(Math.max(1, next), searchPages.value)
  writeUrl()
}

function searchRowTo(row: SearchRow): string {
  return `/audit/${SEGMENT[row.entity as Entity]}/${encodeURIComponent(String(row.key))}`
}

onMounted(() => {
  readUrl()
  if (hasFilters()) void loadSearch()
})

watch(
  () => route.fullPath,
  () => {
    readUrl()
    if (hasFilters()) void loadSearch()
  },
)
</script>

<template>
  <section>
    <div class="status-line">
      <AuditBackLink to="/" label="Overview" />
      <h1>Registry</h1>
    </div>

    <section v-if="canRunPipeline" data-testid="registry-pipeline">
      <h2 style="margin-top: 0">Data pipeline</h2>
      <p class="muted">
        Pull the Zite forms into the raw snapshot, then screen them into the
        final registry. Each run enqueues a durable job; open it for live
        progress and cancellation.
      </p>
      <div
        class="grid"
        style="grid-template-columns: minmax(0, 1fr)"
        data-testid="registry-pipeline-runs"
      >
        <PipelineRunForm command="acquire" submit-label="Run acquire" />
        <PipelineRunForm command="screen" submit-label="Run screen" />
      </div>
    </section>

    <div class="grid" data-testid="audit-sections">
      <AuditNavCard
        v-for="card in cards"
        :key="card.label"
        :label="card.label"
        :to="card.to"
        :total="card.total"
        :done="card.done"
        :done-label="card.doneLabel"
        :statuses="card.statuses"
        :note="card.note"
      />
    </div>

    <h2 data-testid="registry-search">Search the registry</h2>
    <form class="filters" @submit.prevent="applySearch">
      <input
        v-model="query"
        type="search"
        name="q"
        placeholder="Search all fields (fuzzy)"
        aria-label="Search the registry"
      />
      <button type="submit">Search</button>
      <label class="muted">
        Type
        <select v-model="entityScope" @change="applySearch">
          <option value="any">all</option>
          <option value="family">families</option>
          <option value="child">children</option>
          <option value="sponsor">sponsors</option>
        </select>
      </label>
      <label class="muted">
        Audit status
        <select v-model="searchStatus" @change="applySearch">
          <option value="all">any</option>
          <option v-for="option in STATUSES" :key="option" :value="option">
            {{ option.replaceAll('_', ' ') }}
          </option>
        </select>
      </label>
      <label class="muted">
        Location
        <select v-model="searchLocation" @change="applySearch">
          <option value="any">any</option>
          <option value="hancock">hancock</option>
          <option value="calumet">calumet</option>
        </select>
      </label>
      <label class="muted">
        Family size min
        <input v-model="sizeMin" type="number" min="1" @change="applySearch" />
      </label>
      <label class="muted">
        Family size max
        <input v-model="sizeMax" type="number" min="1" @change="applySearch" />
      </label>
      <label class="muted">
        Ignored
        <select v-model="searchIgnore" @change="applySearch">
          <option value="any">any</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
      </label>
      <label class="muted">
        Sponsored
        <select v-model="searchSponsored" @change="applySearch">
          <option value="any">any</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
      </label>
      <span class="muted" data-testid="registry-total">{{ searchTotal }} rows</span>
    </form>

    <p v-if="searchLoading" role="status">Searching…</p>
    <p v-else-if="searchError" class="error-text" role="alert" data-testid="registry-error">
      {{ searchError }} <button type="button" @click="loadSearch">Retry</button>
    </p>
    <p v-else-if="searched && searchRows.length === 0" class="muted" data-testid="registry-empty">
      Nothing in the registry matches.
    </p>

    <table v-else-if="searched" class="data" data-testid="registry-results">
      <thead>
        <tr>
          <th>Type</th>
          <th>Key</th>
          <th>Name</th>
          <th>Status</th>
          <th>Family size</th>
          <th>Indicators</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in searchRows" :key="`${row.entity}-${row.key}`">
          <td class="muted">{{ ENTITY_LABEL[String(row.entity)] ?? row.entity }}</td>
          <td>
            <RouterLink :to="searchRowTo(row)">{{ row.key }}</RouterLink>
          </td>
          <td>{{ row.label }}</td>
          <td><StatusBadge :status="String(row.status ?? 'new')" /></td>
          <td>{{ row.family_size ?? '—' }}</td>
          <td>
            <span v-if="row.ignore === true" class="badge status-ignored">ignored</span>
            <span
              v-if="row.sponsored === true"
              class="badge check-ok"
              data-testid="registry-sponsored"
            >
              sponsored
            </span>
            <span
              v-else-if="row.sponsored === false"
              class="badge check-unchecked"
              data-testid="registry-sponsored"
            >
              unsponsored
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="searched && searchPages > 1" class="pagination">
      <button type="button" :disabled="searchPage <= 1" @click="searchGo(searchPage - 1)">
        Previous
      </button>
      <span data-testid="registry-page">
        Page {{ searchPage }} of {{ searchPages }} ({{ searchTotal }} total)
      </span>
      <button
        type="button"
        :disabled="searchPage >= searchPages"
        @click="searchGo(searchPage + 1)"
      >
        Next
      </button>
    </div>
  </section>
</template>
