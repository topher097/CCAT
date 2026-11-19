<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../api/client'
import { useSessionStore } from '../stores/session'

const session = useSessionStore()
const tab = ref<'duplicates' | 'dob'>('duplicates')
const rows = ref<Record<string, unknown>[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const seasonName = computed(() => session.season?.name ?? '')
const matchReportUrl = computed(
  () =>
    `/api/v1/seasons/${encodeURIComponent(seasonName.value)}/diagnostics/match-report`,
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
onMounted(() => void load())

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
    <h1>Audit</h1>
    <p>
      <router-link to="/audit/sponsors">Sponsors</router-link> ·
      <router-link to="/audit/families">Families</router-link> ·
      <router-link to="/audit/children">Children</router-link> ·
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
