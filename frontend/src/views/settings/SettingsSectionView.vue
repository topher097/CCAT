<script setup lang="ts">
/** /settings/:section — one section's subscreen. */

import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { sectionsFor, sectionBySlug, SETTINGS_SECTIONS } from '../../settings/sections'
import { getAt, type Path } from '../../settings/tree'
import SettingsNode from '../../components/settings/SettingsNode.vue'
import SettingsSectionNav from '../../components/settings/SettingsSectionNav.vue'
import { useSettingsStore } from '../../stores/settings'

const route = useRoute()
const store = useSettingsStore()

const slug = computed(() => String(route.params.section))
const section = computed(() => sectionBySlug(slug.value))
const sections = computed(() =>
  store.draft ? sectionsFor(Object.keys(store.draft)) : SETTINGS_SECTIONS,
)

function nodeKind(key: string): 'scalar' | 'complex' {
  const v = store.draft ? getAt(store.draft, [key]) : undefined
  return v === null || typeof v !== 'object' ? 'scalar' : 'complex'
}

function topPath(key: string): Path {
  return [key]
}
</script>

<template>
  <div
    v-if="!section"
    class="card"
    data-testid="settings-section"
    :data-slug="slug"
  >
    <h2>Section not found</h2>
    <p class="muted">No settings section matches "{{ slug }}".</p>
    <RouterLink to="/settings">Back to settings</RouterLink>
  </div>

  <div
    v-else
    class="section-view"
    data-testid="settings-section"
    :data-slug="slug"
  >
    <nav class="breadcrumb" aria-label="Breadcrumb">
      <RouterLink to="/settings">Settings</RouterLink>
      <span aria-hidden="true">/</span>
      <span>{{ section.title }}</span>
    </nav>

    <header class="section-header">
      <h2>{{ section.title }}</h2>
      <p class="muted">{{ section.summary }}</p>
    </header>

    <SettingsSectionNav :sections="sections" />

    <div class="section-groups">
      <div
        v-for="key in section.keys"
        :key="key"
        :class="['section-key', nodeKind(key)]"
      >
        <div
          v-if="nodeKind(key) === 'scalar'"
          class="card settings-group"
          :data-path="key"
          data-testid="settings-group"
        >
          <SettingsNode :path="topPath(key)" />
        </div>
        <SettingsNode v-else :path="topPath(key)" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.breadcrumb {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-bottom: 0.75rem;
  font-size: 0.95rem;
}

.breadcrumb span:last-child {
  color: #5c6b7a;
}

.section-header {
  margin-bottom: 0.5rem;
}

.section-header h2 {
  margin: 0 0 0.2rem;
  color: #12324f;
}

.section-header p {
  margin: 0;
}

.section-groups {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.section-key.scalar .settings-group {
  margin-bottom: 0;
}
</style>
