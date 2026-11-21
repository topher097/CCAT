<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../api/client'
import { useOverviewStore } from '../stores/overview'
import { useSessionStore } from '../stores/session'
import { STATUSES } from '../utils/status'
import { statusLabel } from '../utils/format'
import AuditBackLink from '../components/AuditBackLink.vue'
import AuditNavCard from '../components/AuditNavCard.vue'

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
const tab = ref<'duplicates' | 'dob'>('duplicates')
const rows = ref<Record<string, unknown>[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const seasonName = computed(() => session.season?.name ?? '')
const matchReportUrl = computed(
  () =>
    `/api/v1/seasons/${encodeURIComponent(seasonName.value)}/diagnostics/match-report`,
)
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

async function load(): Promise<void> {
  if (!seasonName.value) {
    error.value = 'No active season loaded.'
    return
  }
  loading.value = true
  error.value = null
  try {
    rows.value = await api.get<Record<string, unknown>[]>(
      `/api/v1/seasons/${encodeURIComponent(seasonName.value)}/diagnostics/${tab.value}`,
    )
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'diagnostics request failed'
  } finally {
    loading.value = false
  }
}

watch(tab, () => void load())
onMounted(async () => {
  if (!session.season) await session.loadSeason()
  await overview.refresh(seasonName.value)
  void load()
})

/** Columns are whatever the stored log rows carry (server-owned). */
const columns = computed(() => {
  const keys = new Set<string>()
  for (const row of rows.value.slice(0, 5)) {
    for (const key of Object.keys(row)) keys.add(key)
  }
  return [...keys]
})
</script>

<template>
  <section>
    <div class="status-line">
      <AuditBackLink to="/" label="Overview" />
      <h1>Audit</h1>
    </div>

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

    <p class="muted">
      <a :href="matchReportUrl" target="_blank" rel="noopener">
        Match report (JSON)
      </a>
    </p>

    <div class="filters" role="tablist" aria-label="Diagnostics">
      <button
        type="button"
        role="tab"
        :aria-selected="tab === 'duplicates'"
        :aria-pressed="tab === 'duplicates'"
        class="chip"
        @click="tab = 'duplicates'"
      >
        Duplicate submissions
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="tab === 'dob'"
        :aria-pressed="tab === 'dob'"
        class="chip"
        @click="tab = 'dob'"
      >
        DOB conflicts
      </button>
    </div>

    <p v-if="loading" role="status">Loading diagnostics…</p>
    <p v-else-if="error" class="error-text" role="alert">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p v-else-if="rows.length === 0" class="muted" data-testid="diagnostics-empty">
      No {{ tab === 'duplicates' ? 'duplicate groups' : 'DOB conflicts' }}
      recorded.
    </p>
    <div v-else style="overflow-x: auto">
      <table class="data">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="index">
            <td v-for="column in columns" :key="column">
              {{ row[column] ?? '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
