<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useAuditNav } from '../composables/useAuditNav'
import EntityEditor from '../components/EntityEditor.vue'
import AuditBackLink from '../components/AuditBackLink.vue'

const route = useRoute()
const key = computed(() => String(route.params.childId ?? route.params.id ?? ''))
const familyId = computed(() => {
  const value = route.params.familyId
  return typeof value === 'string' && value ? value : null
})
/** The back button returns to the list this child was opened from. */
const { listUrl } = useAuditNav('/audit/children')
</script>

<template>
  <section>
    <EntityEditor entity="child" :entity-key="key">
      <template #back>
        <AuditBackLink
          v-if="familyId"
          :to="`/audit/families/${familyId}`"
          :label="`Family ${familyId}`"
        />
        <AuditBackLink v-else :to="listUrl" label="All children" />
      </template>
    </EntityEditor>
  </section>
</template>
