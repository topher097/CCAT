<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'

import { useOverviewStore } from '../stores/overview'
import { useSessionStore } from '../stores/session'
import { STATUSES } from '../utils/status'
import { formatTime, isStale, statusLabel } from '../utils/format'

const session = useSessionStore()
const overview = useOverviewStore()

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

const ENTITY_CARDS = [
  { entity: 'child', label: 'Children', to: '/audit/children' },
  { entity: 'family', label: 'Families', to: '/audit/families' },
  { entity: 'sponsor', label: 'Sponsors', to: '/audit/sponsors' },
] as const

const counts = computed(
  () =>
    (overview.data?.entities ?? {}) as Record<string, EntityCounts | undefined>,
)

const allEmpty = computed(() =>
  ENTITY_CARDS.every((card) => (counts.value[card.entity]?.total ?? 0) === 0),
)

const revision = computed(() => overview.data?.data_revision ?? null)
const stale = computed(() => isStale(revision.value))
const diagnostics = computed(
  () => (overview.data?.diagnostics ?? {}) as Record<string, number>,
)
const artifacts = computed(
  () => (overview.data?.artifacts ?? {}) as Record<string, number>,
)
const jobs = computed(
  () =>
    (overview.data?.last_jobs ?? {}) as Record<
      string,
      { id?: number; status?: string; created_at?: string }
    >,
)

const seasonName = computed(() => session.season?.name ?? overview.season ?? '')

function retry() {
  void overview.refresh(seasonName.value)
}

onMounted(async () => {
  if (!session.season) await session.loadSeason()
  await overview.refresh(seasonName.value)
  overview.startPolling(seasonName.value)
})

onUnmounted(() => overview.stopPolling())
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1>Season overview</h1>
      <button type="button" :disabled="overview.loading" @click="retry">
        Refresh
      </button>
    </div>

    <p v-if="overview.loading && !overview.data" data-testid="overview-loading">
      Loading overview…
    </p>
    <p
      v-else-if="overview.error"
      class="error-text"
      role="alert"
      data-testid="overview-error"
    >
      Overview failed: {{ overview.error }}.
      <button type="button" @click="retry">Retry</button>
    </p>

    <template v-else-if="overview.data">
      <div
        class="card"
        :class="{ 'diff-changed': stale }"
        data-testid="overview-revision"
      >
        <strong>Data revision:</strong>
        {{ revision ? formatTime(revision) : 'none recorded' }}
        <span v-if="stale" data-testid="overview-stale" class="badge">
          stale — no recent data load/edit
        </span>
        <span v-else class="badge">fresh</span>
        <span class="muted">
          · last loaded {{ formatTime(overview.lastLoaded?.toISOString()) }}
        </span>
      </div>

      <p v-if="allEmpty" class="card" data-testid="overview-empty">
        No data loaded for this season yet. Run an acquire/import job first.
      </p>

      <div class="grid" data-testid="overview-cards" style="margin-top: 1rem">
        <RouterLink
          v-for="card in ENTITY_CARDS"
          :key="card.entity"
          :to="card.to"
          class="card"
          style="text-decoration: none; color: inherit"
        >
          <h2 style="margin-top: 0">{{ card.label }}</h2>
          <p style="font-size: 1.6rem; margin: 0">
            {{ counts[card.entity]?.total ?? 0 }}
          </p>
          <ul style="list-style: none; padding: 0; margin: 0.5rem 0 0">
            <li
              v-for="status in STATUSES"
              :key="status"
              style="display: flex; justify-content: space-between"
            >
              <span>{{ statusLabel(status) }}</span>
              <span>{{
                counts[card.entity]?.by_status?.[status] ?? 0
              }}</span>
            </li>
          </ul>
          <p class="muted" style="margin: 0.5rem 0 0">
            ignored {{ counts[card.entity]?.ignored ?? 0 }} ·
            orphans {{ counts[card.entity]?.orphans ?? 0 }} ·
            raw missing {{ counts[card.entity]?.raw_missing ?? 0 }}
            <template v-if="card.entity === 'child'">
              · pending AI {{ counts[card.entity]?.pending_ai ?? 0 }} · sheets
              generated {{ counts[card.entity]?.generated ?? 0 }}
            </template>
            <template v-else>
              · assigned {{ counts[card.entity]?.assigned ?? 0 }}
            </template>
          </p>
        </RouterLink>

        <RouterLink
          to="/audit"
          class="card"
          style="text-decoration: none; color: inherit"
        >
          <h2 style="margin-top: 0">Diagnostics</h2>
          <ul style="list-style: none; padding: 0; margin: 0">
            <li>
              duplicate groups:
              <strong>{{ diagnostics.duplicate_groups ?? 0 }}</strong>
            </li>
            <li>
              DOB conflicts:
              <strong>{{ diagnostics.dob_conflicts ?? 0 }}</strong>
            </li>
            <li>
              email checks: <strong>{{ diagnostics.email_checks ?? 0 }}</strong>
            </li>
            <li>
              address checks:
              <strong>{{ diagnostics.address_checks ?? 0 }}</strong>
            </li>
          </ul>
        </RouterLink>

        <RouterLink
          to="/generate"
          class="card"
          style="text-decoration: none; color: inherit"
        >
          <h2 style="margin-top: 0">Artifacts</h2>
          <p v-if="Object.keys(artifacts).length === 0" class="muted">
            No artifacts registered.
          </p>
          <ul style="list-style: none; padding: 0; margin: 0">
            <li v-for="(count, kind) in artifacts" :key="kind">
              {{ kind }}: <strong>{{ count }}</strong>
            </li>
          </ul>
        </RouterLink>
      </div>

      <div class="card" style="margin-top: 1rem" data-testid="overview-jobs">
        <h2 style="margin-top: 0">Last job per command</h2>
        <p v-if="Object.keys(jobs).length === 0" class="muted">
          No jobs have run for this season.
        </p>
        <table v-else class="data">
          <thead>
            <tr>
              <th>Command</th>
              <th>Status</th>
              <th>Created</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(job, type) in jobs" :key="type">
              <td>{{ type }}</td>
              <td>{{ job.status ?? 'unknown' }}</td>
              <td>{{ formatTime(job.created_at) }}</td>
              <td>
                <RouterLink :to="`/jobs?job=${job.id ?? ''}`">open job</RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
