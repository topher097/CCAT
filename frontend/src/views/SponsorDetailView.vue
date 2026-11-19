<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import EntityEditor from '../components/EntityEditor.vue'

const route = useRoute()
const key = computed(() => String(route.params.id ?? ''))
</script>

<template>
  <section>
    <p><RouterLink to="/audit/sponsors">← All sponsors</RouterLink></p>
    <EntityEditor entity="sponsor" :entity-key="key" />
  </section>
</template>
