<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta.title ?? 'Section')
</script>

<template>
  <section class="card">
    <h1>{{ title }}</h1>
    <p class="muted">
      Route scaffolded in Phase 4; the full workflow lands in Phase 4b after
      the audit area reaches parity.
    </p>
  </section>
</template>
