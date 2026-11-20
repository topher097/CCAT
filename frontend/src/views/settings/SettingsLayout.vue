<script setup lang="ts">
/** /settings shared chrome: load, gate, save bar, error banner. */

import { computed, onMounted } from 'vue'
import { RouterLink, RouterView } from 'vue-router'

import { useSessionStore } from '../../stores/session'
import { useSettingsStore } from '../../stores/settings'
import { useDirtyGuard } from '../../composables/useDirtyGuard'

const session = useSessionStore()
const store = useSettingsStore()
const isAdmin = computed(() => session.hasRole('admin'))

const dirty = computed(() => store.dirty)
useDirtyGuard(dirty)

onMounted(() => {
  if (isAdmin.value && !store.draft) {
    void store.load()
  }
})
</script>

<template>
  <section class="settings-layout">
    <h1>Settings</h1>

    <p v-if="!isAdmin" class="muted" data-testid="settings-forbidden">
      Admin role required. <RouterLink to="/">Back to overview</RouterLink>
    </p>

    <template v-else>
      <p v-if="store.loading" role="status">Loading configuration…</p>

      <p
        v-else-if="store.error && !store.draft"
        class="error-text"
        role="alert"
        data-testid="settings-load-error"
      >
        {{ store.error }} <button type="button" @click="store.load">Retry</button>
      </p>

      <template v-else-if="store.draft">
        <p class="muted file-path">
          File: <code>{{ store.filePath }}</code>
          <span v-if="store.dirty" class="badge">Unsaved changes</span>
        </p>

        <RouterView />

        <div class="save-bar">
          <button
            type="button"
            class="primary"
            data-testid="settings-save"
            :disabled="store.saving || !store.dirty"
            @click="store.save"
          >
            {{ store.saving ? 'Saving…' : 'Save' }}
          </button>
          <button
            type="button"
            data-testid="settings-discard"
            :disabled="!store.dirty"
            @click="store.discard"
          >
            Discard
          </button>
        </div>

        <p
          v-if="store.error"
          class="error-text"
          role="alert"
          data-testid="settings-error"
        >
          {{ store.error }}
          <span
            v-for="(message, errPath) in store.fieldErrors"
            :key="errPath"
            class="error-path"
          >
            {{ errPath }} — {{ message }}
          </span>
        </p>

        <p
          v-if="store.savedAt && !store.dirty && !store.error"
          class="notice"
          role="status"
          data-testid="settings-saved"
        >
          Settings saved.
        </p>
      </template>
    </template>
  </section>
</template>

<style scoped>
.settings-layout h1 {
  margin-top: 0;
  color: #12324f;
}

.file-path {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 1rem;
}

.file-path code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
    'Liberation Mono', monospace;
  background: #eef2f6;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
}

.save-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  gap: 0.75rem;
  padding: 0.75rem 0;
  margin-top: 1rem;
  background: #f5f7fa;
  border-top: 1px solid #d7dee6;
}

.error-path {
  display: block;
  margin-top: 0.25rem;
}

.notice {
  margin-top: 0.75rem;
}
</style>
