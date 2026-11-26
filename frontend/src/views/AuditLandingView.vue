<script setup lang="ts">
import { computed, onMounted } from 'vue'

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

onMounted(async () => {
  if (!session.season) await session.loadSeason()
  await overview.refresh(seasonName.value)
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
  </section>
</template>
