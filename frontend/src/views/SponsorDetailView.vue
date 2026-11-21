<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useAuditNav } from '../composables/useAuditNav'
import EntityEditor from '../components/EntityEditor.vue'
import AuditBackLink from '../components/AuditBackLink.vue'

const route = useRoute()
const key = computed(() => String(route.params.id ?? ''))
/** The back button returns to the sponsors list this sponsor came from. */
const { listUrl } = useAuditNav('/audit/sponsors')
</script>

<template>
  <section>
    <EntityEditor entity="sponsor" :entity-key="key">
      <template #back>
        <AuditBackLink :to="listUrl" label="All sponsors" />
      </template>
    </EntityEditor>
  </section>
</template>
