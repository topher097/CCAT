<script setup lang="ts">
/** /acquire and /screen: one pipeline run form plus job links (§7.1.8). */

import { RouterLink } from 'vue-router'

import PipelineRunForm from '../components/PipelineRunForm.vue'

defineProps<{ command: 'acquire' | 'screen' }>()
</script>

<template>
  <section>
    <h1 style="margin-top: 0">
      {{ command === 'acquire' ? 'Acquire' : 'Screen' }}
    </h1>
    <p class="muted">
      Runs enqueue a durable job; open it for live progress and cancellation.
      <RouterLink to="/jobs">Job center</RouterLink>
    </p>
    <PipelineRunForm :command="command" />
  </section>
</template>
