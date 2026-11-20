<script setup lang="ts">
/** /generate: run form, generation artifacts, and per-child markers. */

import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { listEntities } from '../api/audit'
import {
  artifactDownloadUrl,
  listArtifacts,
  type Artifact,
} from '../api/commands'
import { useSessionStore } from '../stores/session'
import { formatTime } from '../utils/format'
import PipelineRunForm from '../components/PipelineRunForm.vue'

const session = useSessionStore()

const kind = ref('all')
const artifacts = ref<Artifact[]>([])
const children = ref<Record<string, unknown>[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const markerError = ref<string | null>(null)

const seasonName = computed(() => session.season?.name ?? '')
const generatedCount = computed(
  () => children.value.filter((row) => row.generated_info_sheet === true).length,
)

async function loadArtifacts(): Promise<void> {
  if (!seasonName.value) return
  loading.value = true
  error.value = null
  try {
    artifacts.value = await listArtifacts(
      seasonName.value,
      kind.value === 'all' ? undefined : kind.value,
    )
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'artifact request failed'
  } finally {
    loading.value = false
  }
}

async function loadMarkers(): Promise<void> {
  if (!seasonName.value) return
  markerError.value = null
  try {
    const page = await listEntities(seasonName.value, 'child', {
      page_size: 200,
    })
    children.value = page.items as Record<string, unknown>[]
  } catch (failure) {
    markerError.value =
      failure instanceof Error ? failure.message : 'child request failed'
  }
}

function sizeLabel(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

watch(kind, () => void loadArtifacts())
onMounted(() => {
  void loadArtifacts()
  void loadMarkers()
})
</script>

<template>
  <section>
    <h1 style="margin-top: 0">Generate</h1>
    <p class="muted">
      Runs enqueue a durable job; open it for live progress.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>

    <PipelineRunForm command="generate" submit-label="Run generation" />

    <section class="card" style="margin-top: 1rem">
      <div class="status-line" style="justify-content: space-between">
        <h2 style="margin: 0">Artifacts</h2>
        <button type="button" :disabled="loading" @click="loadArtifacts">
          Refresh
        </button>
      </div>
      <div class="filters">
        <label>
          Kind
          <select v-model="kind" aria-label="Artifact kind filter">
            <option value="all">all generation artifacts</option>
            <option value="child_sheet">child sheets</option>
            <option value="sponsor_packet">sponsor packets</option>
          </select>
        </label>
      </div>
      <p v-if="loading" role="status">Loading artifacts…</p>
      <p v-else-if="error" class="error-text" role="alert">
        {{ error }} <button type="button" @click="loadArtifacts">Retry</button>
      </p>
      <p
        v-else-if="artifacts.length === 0"
        class="muted"
        data-testid="artifacts-empty"
      >
        No generation artifacts yet.
      </p>
      <table v-else class="data" data-testid="artifact-list">
        <thead>
          <tr>
            <th>Kind</th>
            <th>File</th>
            <th>Child</th>
            <th>Size</th>
            <th>Created</th>
            <th>Download</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="artifact in artifacts" :key="artifact.id">
            <td>{{ artifact.kind?.replaceAll('_', ' ') ?? '—' }}</td>
            <td>{{ artifact.filename ?? '—' }}</td>
            <td>{{ artifact.meta?.zite_record_id ?? '—' }}</td>
            <td>{{ sizeLabel(artifact.size_bytes) }}</td>
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

      <h2>Child generation state ({{ generatedCount }} generated)</h2>
      <p v-if="markerError" class="error-text" role="alert">
        {{ markerError }}
      </p>
      <p v-else-if="children.length === 0" class="muted">No children stored.</p>
      <table v-else class="data" data-testid="child-markers">
        <thead>
          <tr>
            <th>Child</th>
            <th>Name</th>
            <th>Family</th>
            <th>Info sheet</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in children" :key="String(row.zite_record_id)">
            <td>
              <RouterLink :to="`/audit/children/${row.zite_record_id}`">
                {{ row.zite_record_id }}
              </RouterLink>
            </td>
            <td>{{ row.first_name ?? '—' }}</td>
            <td>{{ row.family_id ?? '—' }}</td>
            <td>
              <span
                class="badge"
                :class="
                  row.generated_info_sheet === true
                    ? 'status-audited'
                    : 'status-new'
                "
              >
                {{
                  row.generated_info_sheet === true
                    ? 'sheet generated'
                    : 'pending'
                }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>
