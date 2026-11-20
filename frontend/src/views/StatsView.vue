<script setup lang="ts">
/** /stats: report run form and artifact downloads, incl. partial state. */

import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  artifactDownloadUrl,
  listArtifacts,
  type Artifact,
} from '../api/commands'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import PipelineRunForm from '../components/PipelineRunForm.vue'

const session = useSessionStore()

const artifacts = ref<Artifact[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const seasonName = computed(() => session.season?.name ?? '')

async function load(): Promise<void> {
  if (!seasonName.value) return
  loading.value = true
  error.value = null
  try {
    artifacts.value = await listArtifacts(seasonName.value, 'stats_report')
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'artifact request failed'
  } finally {
    loading.value = false
  }
}

/** HTML kept without a PDF is an explicit partial report (§9.6). */
function incomplete(artifact: Artifact): boolean {
  return artifact.meta?.complete === false
}

function kindLabel(artifact: Artifact): string {
  const name = artifact.filename ?? ''
  if (name.toLowerCase().endsWith('.pdf')) return 'PDF'
  if (name.toLowerCase().endsWith('.html')) return 'HTML'
  return artifact.content_type ?? 'report'
}

onMounted(() => void load())
</script>

<template>
  <section>
    <h1 style="margin-top: 0">Stats</h1>
    <p class="muted">
      The report reads the eligible persistent-final population by default;
      prepared source mode reads an ephemeral cleaned snapshot instead.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>

    <PipelineRunForm command="stats" submit-label="Run stats report" />

    <section class="card" style="margin-top: 1rem">
      <div class="status-line" style="justify-content: space-between">
        <h2 style="margin: 0">Reports</h2>
        <button type="button" :disabled="loading" @click="load">Refresh</button>
      </div>
      <p v-if="loading" role="status">Loading reports…</p>
      <p v-else-if="error" class="error-text" role="alert">
        {{ error }} <button type="button" @click="load">Retry</button>
      </p>
      <p
        v-else-if="artifacts.length === 0"
        class="muted"
        data-testid="stats-empty"
      >
        No statistics reports yet.
      </p>
      <table v-else class="data" data-testid="stats-artifacts">
        <thead>
          <tr>
            <th>Format</th>
            <th>File</th>
            <th>Source</th>
            <th>State</th>
            <th>Created</th>
            <th>Download</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="artifact in artifacts" :key="artifact.id">
            <td>{{ kindLabel(artifact) }}</td>
            <td>{{ artifact.filename ?? '—' }}</td>
            <td class="muted">
              {{ artifact.meta?.mode === 'raw' ? 'prepared source' : 'final' }}
            </td>
            <td>
              <span
                v-if="incomplete(artifact)"
                class="badge status-needs-clarification"
                data-testid="stats-incomplete"
              >
                incomplete report
              </span>
              <span v-else class="badge status-audited">complete</span>
            </td>
            <td class="muted">{{ formatTime(artifact.created_at) }}</td>
            <td>
              <a
                :href="artifactDownloadUrl(artifact.id)"
                data-testid="artifact-download"
              >
                Download
              </a>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>
