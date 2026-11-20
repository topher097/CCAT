<script setup lang="ts">
/** /admin/database: live SQL console (admin only). */

import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError } from '../api/client'
import { listTables, runQuery, type QueryResult, type Table } from '../api/dbconsole'
import { useSessionStore } from '../stores/session'

const session = useSessionStore()
const isAdmin = computed(() => session.hasRole('admin'))

const tables = ref<Table[]>([])
const sql = ref('')
const write = ref(false)
const running = ref(false)
const loading = ref(false)
const error = ref<string | null>(null)
const result = ref<QueryResult | null>(null)

async function loadTables(): Promise<void> {
  if (!isAdmin.value) return
  loading.value = true
  error.value = null
  try {
    tables.value = await listTables()
  } catch (failure) {
    error.value =
      failure instanceof ApiError ? failure.message : 'table request failed'
  } finally {
    loading.value = false
  }
}

function selectTable(event: Event): void {
  const name = (event.target as HTMLSelectElement).value
  if (name) sql.value = `SELECT * FROM "${name}" LIMIT 200`
}

async function run(): Promise<void> {
  error.value = null
  result.value = null
  running.value = true
  try {
    result.value = await runQuery({ sql: sql.value, write: write.value })
  } catch (failure) {
    error.value =
      failure instanceof ApiError ? failure.message : 'query request failed'
  } finally {
    running.value = false
  }
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

onMounted(() => void loadTables())
</script>

<template>
  <section>
    <h1 style="margin-top: 0">Database</h1>

    <p v-if="!isAdmin" class="muted" data-testid="db-forbidden">
      Admin role required. <RouterLink to="/">Back to overview</RouterLink>
    </p>

    <template v-else>
      <p class="muted">
        Runs against the live database. Every statement is recorded in the
        security audit log.
      </p>

      <section class="card">
        <div class="filters">
          <label>
            Table
            <select data-testid="db-tables" @change="selectTable">
              <option value="">Choose a table…</option>
              <option v-for="table in tables" :key="table.name" :value="table.name">
                {{ table.name }} ({{ table.row_count }})
              </option>
            </select>
          </label>
          <span v-if="loading" class="muted" role="status">Loading tables…</span>
        </div>

        <p>
          <textarea
            v-model="sql"
            data-testid="db-sql"
            rows="6"
            style="width: 100%; font-family: monospace"
            placeholder='SELECT * FROM "final_child" LIMIT 200'
          />
        </p>

        <p>
          <label>
            <input v-model="write" type="checkbox" data-testid="db-write" />
            Allow writes
          </label>
        </p>
        <p v-if="write" class="error-text" role="alert">
          Writes enabled — this statement can change data.
        </p>

        <button
          type="button"
          class="primary"
          data-testid="db-run"
          :disabled="running"
          @click="run"
        >
          {{ running ? 'Running…' : 'Run' }}
        </button>
      </section>

      <p v-if="error" class="error-text" role="alert" data-testid="db-error">
        {{ error }}
      </p>

      <template v-if="result">
        <p class="muted" role="status">
          {{ result.row_count }} row(s) returned, {{ result.affected_rows }}
          affected, {{ result.duration_ms }} ms
          <span v-if="result.truncated"> — truncated, more rows exist</span>
        </p>
        <table
          v-if="result.columns?.length"
          class="data"
          data-testid="db-results"
        >
          <thead>
            <tr>
              <th v-for="column in result.columns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in result.rows ?? []" :key="index">
              <td v-for="column in result.columns" :key="column">
                {{ cellText(row[column]) }}
              </td>
            </tr>
          </tbody>
        </table>
      </template>
    </template>
  </section>
</template>
