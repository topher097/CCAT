<script setup lang="ts">
/** Compact horizontal section list for jumping between subscreens. */

import { RouterLink } from 'vue-router'

import type { SettingsSection } from '../../settings/sections'

defineProps<{
  sections: SettingsSection[]
}>()
</script>

<template>
  <nav class="section-nav" data-testid="settings-sections" aria-label="Sections">
    <RouterLink
      v-for="section in sections"
      :key="section.slug"
      :to="`/settings/${section.slug}`"
      class="section-link"
      data-testid="settings-section-link"
      :data-slug="section.slug"
    >
      {{ section.title }}
    </RouterLink>
  </nav>
</template>

<style scoped>
.section-nav {
  display: flex;
  gap: 0.35rem;
  padding: 0.5rem 0;
  margin-bottom: 1rem;
  overflow-x: auto;
  white-space: nowrap;
  border-bottom: 1px solid #e2e8ee;
}

.section-link {
  padding: 0.35rem 0.65rem;
  border-radius: 4px;
  text-decoration: none;
  font-size: 0.9rem;
  color: #0b5cad;
}

.section-link.router-link-active {
  background: #dbe8f5;
  font-weight: 600;
}
</style>
