<script setup lang="ts">
/** /settings: scannable cards for every section. */

import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { sectionsFor, SETTINGS_SECTIONS } from '../../settings/sections'
import { useSettingsStore } from '../../stores/settings'

const store = useSettingsStore()

const sections = computed(() =>
  store.draft ? sectionsFor(Object.keys(store.draft)) : SETTINGS_SECTIONS,
)

function keyPreview(keys: string[]): string {
  if (keys.length === 0) return ''
  const shown = keys.slice(0, 3)
  const rest = keys.length - shown.length
  return rest > 0 ? `${shown.join(', ')} +${rest}` : shown.join(', ')
}
</script>

<template>
  <div class="settings-index">
    <header>
      <p class="muted">Choose a section to edit.</p>
    </header>

    <div class="grid" data-testid="settings-sections">
      <RouterLink
        v-for="section in sections"
        :key="section.slug"
        :to="`/settings/${section.slug}`"
        class="card section-link"
        data-testid="settings-section-link"
        :data-slug="section.slug"
      >
        <h3>{{ section.title }}</h3>
        <p class="muted summary">{{ section.summary }}</p>
        <p class="muted keys">{{ keyPreview(section.keys) }}</p>
        <span class="badge">{{ section.keys.length }} keys</span>
      </RouterLink>
    </div>
  </div>
</template>

<style scoped>
.settings-index header {
  margin-bottom: 1rem;
}
.section-link {
  display: flex;
  flex-direction: column;
  text-decoration: none;
  color: inherit;
  transition: box-shadow 0.15s ease, border-color 0.15s ease;
}

.section-link:hover {
  border-color: #0b5cad;
  box-shadow: 0 4px 12px rgba(11, 92, 173, 0.1);
}

.section-link h3 {
  margin: 0 0 0.35rem;
  color: #0b5cad;
  font-size: 1.15rem;
}

.summary {
  margin: 0 0 0.5rem;
  flex: 1 1 auto;
}

.keys {
  margin: 0 0 0.6rem;
  font-size: 0.85rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
    'Liberation Mono', monospace;
}

.badge {
  align-self: flex-start;
}
</style>
