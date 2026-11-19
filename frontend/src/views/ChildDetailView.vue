<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import EntityEditor from '../components/EntityEditor.vue'

const route = useRoute()
const key = computed(() => String(route.params.childId ?? route.params.id ?? ''))
const familyId = computed(() => {
  const value = route.params.familyId
  return typeof value === 'string' && value ? value : null
})
</script>

<template>
  <section>
    <p>
      <RouterLink v-if="familyId" :to="`/audit/families/${familyId}`">
        ← Family {{ familyId }}
      </RouterLink>
      <RouterLink v-else to="/audit/children">← All children</RouterLink>
    </p>
    <EntityEditor entity="child" :entity-key="key" />
  </section>
</template>
